# Copyright 2022 TIER IV, INC. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from __future__ import annotations

import concurrent.futures.thread as concurrent_fut_thread
import contextlib
import itertools
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from queue import Empty, SimpleQueue
from typing import TYPE_CHECKING, Any, Callable, Generator, Iterable, Optional

from typing_extensions import ParamSpec

from otaclient_common.common import wait_with_backoff
from otaclient_common.typing import RT, T

P = ParamSpec("P")

logger = logging.getLogger(__name__)


class TasksEnsureFailed(Exception):
    """Exception for tasks ensuring failed."""


CONTINUES_FAILURE_COUNT_ATTRNAME = "continues_failed_count"


class _ThreadPoolExecutorWithRetry(ThreadPoolExecutor):

    def __init__(
        self,
        max_concurrent: int,
        max_workers: Optional[int] = None,
        max_total_retry: Optional[int] = None,
        thread_name_prefix: str = "",
        watchdog_func: Optional[Callable] = None,
        watchdog_check_interval: int = 3,  # seconds
        initializer: Callable[..., Any] | None = None,
        initargs: tuple = (),
        backoff_factor: float = 0.01,
        backoff_max: float = 1,
    ) -> None:
        self._start_lock, self._started = threading.Lock(), False
        self._total_task_num = 0
        """
        NOTE:
            1. when is 0, the tasks dispatch is not yet started.
            2. when becomes -1, it means that the input tasks iterable yields
                no tasks, the task execution gen should stop immediately.
            3. only value >=1 is valid.
        """
        self.backoff_factor = backoff_factor
        self.backoff_max = backoff_max

        self._retry_counter = itertools.count(start=1)
        self._retry_count = 0
        self._concurrent_semaphore = threading.Semaphore(max_concurrent)
        self._fut_queue: SimpleQueue[Future[Any]] = SimpleQueue()

        self._watchdog_check_interval = watchdog_check_interval
        self._checker_funcs: list[Callable[[], Any]] = []
        if isinstance(max_total_retry, int) and max_total_retry > 0:
            self._checker_funcs.append(partial(self._max_retry_check, max_total_retry))
        if callable(watchdog_func):
            self._checker_funcs.append(watchdog_func)

        self._thread_local = threading.local()
        super().__init__(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
            initializer=self._rtm_initializer_gen(initializer),
            initargs=initargs,
        )

    def _rtm_initializer_gen(
        self, _input_initializer: Callable[P, RT] | None
    ) -> Callable[P, None]:
        def _real_initializer(*args: P.args, **kwargs: P.kwargs) -> None:
            _thread_local = self._thread_local
            setattr(_thread_local, CONTINUES_FAILURE_COUNT_ATTRNAME, 0)

            if callable(_input_initializer):
                _input_initializer(*args, **kwargs)

        return _real_initializer

    def _max_retry_check(self, max_total_retry: int) -> None:
        if self._retry_count > max_total_retry:
            raise TasksEnsureFailed("exceed max retry count, abort")

    def _watchdog(
        self,
        _checker_funcs: list[Callable[[], Any]],
        interval: int,
    ) -> None:
        """Watchdog will shutdown the threadpool on certain conditions being met."""
        while not (self._shutdown or self._broken or concurrent_fut_thread._shutdown):
            time.sleep(interval)
            try:
                for _func in _checker_funcs:
                    _func()
            except Exception as e:
                logger.warning(
                    f"watchdog failed: {e!r}, shutdown the pool and draining the workitem queue on shutdown.."
                )
                self.shutdown(wait=False)
                # drain the worker queues to cancel all the futs
                with contextlib.suppress(Empty):
                    while True:
                        self._work_queue.get_nowait()

    def _task_done_cb(
        self, fut: Future[Any], /, *, item: T, func: Callable[[T], Any]
    ) -> None:
        if self._shutdown or self._broken or concurrent_fut_thread._shutdown:
            self._concurrent_semaphore.release()  # on shutdown, always release a se
            return  # on shutdown, no need to put done fut into fut_queue
        self._fut_queue.put_nowait(fut)

        _thread_local = self._thread_local

        # release semaphore only on success
        # reset continues failure count on success
        if not fut.exception():
            self._concurrent_semaphore.release()
            _thread_local.continues_failed_count = 0
            return

        # NOTE: when for some reason the continues_failed_count is gone,
        #   handle the AttributeError here and re-assign the count.
        try:
            _continues_failed_count = getattr(
                _thread_local, CONTINUES_FAILURE_COUNT_ATTRNAME
            )
        except AttributeError:
            _continues_failed_count = 0

        _continues_failed_count += 1
        setattr(
            _thread_local, CONTINUES_FAILURE_COUNT_ATTRNAME, _continues_failed_count
        )
        wait_with_backoff(
            _continues_failed_count,
            _backoff_factor=self.backoff_factor,
            _backoff_max=self.backoff_max,
        )

        self._retry_count = next(self._retry_counter)
        try:  # try to re-schedule the failed task
            self.submit(func, item).add_done_callback(
                partial(self._task_done_cb, item=item, func=func)
            )
        except Exception:  # if re-schedule doesn't happen, release se
            self._concurrent_semaphore.release()

    def _fut_gen(self, interval: int) -> Generator[Future[Any], Any, None]:
        """Generator which yields the done future, controlled by the caller."""
        finished_tasks = 0
        while finished_tasks == 0 or finished_tasks != self._total_task_num:
            if self._total_task_num < 0:
                return

            if self._shutdown or self._broken or concurrent_fut_thread._shutdown:
                logger.warning(
                    f"dispatcher exits on threadpool shutdown, {finished_tasks=}, {self._total_task_num=}"
                )
                with contextlib.suppress(Empty):
                    while True:  # drain the _fut_queue
                        self._fut_queue.get_nowait()
                raise TasksEnsureFailed  # raise exc to upper caller

            try:
                done_fut = self._fut_queue.get_nowait()
                if not done_fut.exception():
                    finished_tasks += 1
                yield done_fut
            except Empty:
                time.sleep(interval)

    def ensure_tasks(
        self,
        func: Callable[[T], RT],
        iterable: Iterable[T],
        *,
        ensure_tasks_pull_interval: int = 1,
    ) -> Generator[Future[RT], None, None]:
        with self._start_lock:
            if self._started:
                try:
                    raise ValueError("ensure_tasks cannot be started more than once")
                finally:  # do not hold refs to input params
                    del self, func, iterable
            self._started = True

        if self._checker_funcs:
            threading.Thread(
                target=self._watchdog,
                args=(self._checker_funcs, self._watchdog_check_interval),
                daemon=True,
            ).start()

        # ------ dispatch tasks from iterable ------ #
        def _dispatcher() -> None:
            _tasks_count = -1  # means no task is scheduled
            try:
                for _tasks_count, item in enumerate(iterable, start=1):
                    if (
                        self._shutdown
                        or self._broken
                        or concurrent_fut_thread._shutdown
                    ):
                        logger.warning("threadpool is closing, exit")
                        return  # directly exit on shutdown

                    self._concurrent_semaphore.acquire()
                    fut = self.submit(func, item)
                    fut.add_done_callback(
                        partial(self._task_done_cb, item=item, func=func)
                    )
            except Exception as e:
                logger.error(f"tasks dispatcher failed: {e!r}, abort")
                self.shutdown(wait=False)
                return

            self._total_task_num = _tasks_count
            if _tasks_count > 0:
                logger.info(f"finish dispatch {_tasks_count} tasks")
            else:
                logger.warning("no task is scheduled!")

        threading.Thread(target=_dispatcher, daemon=True).start()

        # ------ ensure all tasks are finished ------ #
        # NOTE: also see base.Executor.map method, let the yield hidden in
        #   a generator so that the first fut will be dispatched before
        #   we start to get from fut_queue.
        return self._fut_gen(ensure_tasks_pull_interval)


# only expose APIs we want to exposed
if TYPE_CHECKING:

    class ThreadPoolExecutorWithRetry:

        def __init__(
            self,
            max_concurrent: int,
            max_workers: Optional[int] = None,
            max_total_retry: Optional[int] = None,
            thread_name_prefix: str = "",
            watchdog_func: Optional[Callable] = None,
            watchdog_check_interval: int = 3,  # seconds
            initializer: Callable[..., Any] | None = None,
            initargs: tuple = (),
            backoff_factor: float = 0.01,
            backoff_max: float = 1,
        ) -> None:
            """Initialize a ThreadPoolExecutorWithRetry instance.

            Args:
                max_concurrent (int): Limit the number pending scheduled tasks.
                max_workers (Optional[int], optional): Max number of worker threads in the pool. Defaults to None.
                max_total_retry (Optional[int], optional): Max total retry counts before abort. Defaults to None.
                thread_name_prefix (str, optional): Defaults to "".
                watchdog_func (Optional[Callable]): A custom func to be called on watchdog thread, when
                    this func raises exception, the watchdog will interrupt the tasks execution. Defaults to None.
                watchdog_check_interval (int): Defaults to 3(seconds).
                initializer (Callable[..., Any] | None): The same <initializer> param passed through to ThreadPoolExecutor.
                    Defaults to None.
                initargs (tuple): The same <initargs> param passed through to ThreadPoolExecutor.
                    Defaults to ().
                backoff_factor (float): The backoff factor on thread-scope backoff wait for failed tasks rescheduling.
                    Defaults to 0.01.
                backoff_max (float): The backoff max on thread-scope backoff wait for failed tasks rescheduling.
                    Defaults to 1.
            """
            raise NotImplementedError

        def ensure_tasks(
            self,
            func: Callable[[T], RT],
            iterable: Iterable[T],
            *,
            ensure_tasks_pull_interval: int = 1,
        ) -> Generator[Future[RT], None, None]:
            """Ensure all the items in <iterable> are processed by <func> in the pool.

            Args:
                func (Callable[[T], RT]): The function to take the item from <iterable>.
                iterable (Iterable[T]): The iterable of items to be processed by <func>.

            Raises:
                ValueError: If the pool is shutdown or broken, or this method has already
                    being called once.
                TasksEnsureFailed: If failed to ensure all the tasks are finished.

            Yields:
                The Future instance of each processed tasks.
            """
            raise NotImplementedError

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            raise NotImplementedError

else:
    ThreadPoolExecutorWithRetry = _ThreadPoolExecutorWithRetry
