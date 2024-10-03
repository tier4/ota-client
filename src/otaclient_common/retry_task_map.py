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

from otaclient_common.typing import RT, T

logger = logging.getLogger(__name__)


class TasksEnsureFailed(Exception):
    """Exception for tasks ensuring failed."""


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

        super().__init__(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
            initializer=initializer,
            initargs=initargs,
        )

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

        # ------ on task failed ------ #
        if fut.exception():
            self._retry_count = next(self._retry_counter)
            try:  # try to re-schedule the failed task
                self.submit(func, item).add_done_callback(
                    partial(self._task_done_cb, item=item, func=func)
                )
            except Exception:  # if re-schedule doesn't happen, release se
                self._concurrent_semaphore.release()
        else:  # release semaphore when succeeded
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
