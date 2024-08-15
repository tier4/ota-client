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

import atexit
import itertools
import logging
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from queue import Empty, SimpleQueue
from typing import Any, Callable, Generator, Iterable, Optional

from otaclient_common.common import get_backoff
from otaclient_common.typing import RT, T

logger = logging.getLogger(__name__)

# for waking up any background threads created by this module.
_shutdown = False


def _python_exit():
    global _shutdown
    _shutdown = True


# see concurrent.futures.thread module for more details.
if sys.version_info >= (3, 9, 0):
    threading._register_atexit(_python_exit)
else:
    atexit.register(_python_exit)


class TasksEnsureFailed(Exception):
    """Exception for tasks ensuring failed."""


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
        waiton_failure_counts_threshold: int = 16,
        waiton_interval: int | Callable[[int], int | float] = partial(
            get_backoff, factor=0.01, _max=1
        ),
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
            waiton_failure_counts_threshold (int): Threshold of continues failures before starting to wait with backoff
                on exceeding failures above the threshold, only meaningful when waiton_interval is not None. Defaults to 16.
            waiton_interval (int | Callable[[int], int] | None): An int of static wait time, or a callable that takes an
                int of continues failures and returns wait time in int, or None to disable the wait on continues failures.
                Defaults to backoff with factor==0.1, max==1.
        """
        self._start_lock, self._started = threading.Lock(), False
        self._total_task_num = 0
        self._retry_counter = itertools.count(start=1)
        self._retry_count = 0
        self._concurrent_semaphore = threading.Semaphore(max_concurrent)
        self._fut_queue: SimpleQueue[Future[Any]] = SimpleQueue()

        self._waiton_failure_counts_threshold = waiton_failure_counts_threshold
        if isinstance(waiton_interval, int):

            def _waiton_interval(_: int):
                return waiton_interval

            self._waiton_interval = _waiton_interval

        else:
            self._waiton_interval = waiton_interval

        # NOTE: no need to ensure the atomic assignment for this property,
        #   this is only for backoff waiting on continues failures,
        #   a dirty access to this property only causes unharmful extra waiting time.
        self._continues_failure = 0

        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
            initializer=initializer,
            initargs=initargs,
        )

        if max_total_retry or callable(watchdog_func):
            threading.Thread(
                target=self._watchdog,
                args=(max_total_retry, watchdog_func, watchdog_check_interval),
                daemon=True,
            ).start()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._executor.shutdown(wait=True)
        return False

    def _watchdog(
        self,
        max_retry: int | None,
        watchdog_func: Callable[..., Any] | None,
        interval: int,
    ) -> None:
        """Watchdog will shutdown the threadpool on certain conditions being met."""
        executor = self._executor
        while not executor._shutdown and not _shutdown:
            if max_retry and self._retry_count > max_retry:
                logger.warning(f"exceed {max_retry=}, abort")
                return executor.shutdown(wait=True)

            if callable(watchdog_func):
                try:
                    watchdog_func()
                except Exception as e:
                    logger.warning(f"custom watchdog func failed: {e!r}, abort")
                    return executor.shutdown(wait=True)
            time.sleep(interval)

    def _task_done_cb(
        self, fut: Future[Any], /, *, item: T, func: Callable[[T], Any]
    ) -> None:
        self._concurrent_semaphore.release()  # always release se first
        self._fut_queue.put_nowait(fut)

        # ------ on task succeeded ------ #
        if fut.exception() is None:
            if self._continues_failure:
                self._continues_failure = 0
            return

        # ------ on task failed ------ #
        self._continues_failure += 1
        self._retry_count = next(self._retry_counter)
        try:  # on threadpool shutdown
            # wait with backoff before putting the failed task back to queue
            if (
                self._waiton_interval
                and (
                    exceed_failures := self._continues_failure
                    - self._waiton_failure_counts_threshold
                )
                > 0
            ):
                time.sleep(self._waiton_interval(exceed_failures))

            self._executor.submit(func, item).add_done_callback(
                partial(self._task_done_cb, item=item, func=func)
            )
        except Exception:
            del fut, item, func  # remove refs on failed submit

    def _fut_gen(self, interval: int) -> Generator[Future[Any], Any, None]:
        finished_tasks = 0
        executor = self._executor
        while True:
            if executor._shutdown or _shutdown:
                logger.warning(
                    f"failed to ensure all tasks, {finished_tasks=}, {self._total_task_num=}"
                )
                raise TasksEnsureFailed

            try:
                done_fut = self._fut_queue.get_nowait()
                if not done_fut.exception():
                    finished_tasks += 1
                yield done_fut
            except Empty:
                if self._total_task_num == 0 or finished_tasks != self._total_task_num:
                    time.sleep(interval)
                    continue
                return

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
        with self._start_lock:
            if self._started:
                try:
                    raise ValueError("ensure_tasks cannot be started more than once")
                finally:  # do not hold refs to input params
                    del self, func, iterable
            self._started = True

        # ------ dispatch tasks from iterable ------ #
        def _dispatcher() -> None:
            executor = self._executor
            try:
                for _tasks_count, item in enumerate(iterable, start=1):
                    self._concurrent_semaphore.acquire()
                    fut = executor.submit(func, item)
                    fut.add_done_callback(
                        partial(self._task_done_cb, item=item, func=func)
                    )
            except Exception as e:
                logger.error(f"tasks dispatcher failed: {e!r}, abort")
                executor.shutdown(wait=True)
                return
            finally:
                executor = None  # remove ref

            self._total_task_num = _tasks_count
            logger.info(f"finish dispatch {_tasks_count} tasks")

        threading.Thread(target=_dispatcher, daemon=True).start()

        # ------ ensure all tasks are finished ------ #
        # NOTE: also see base.Executor.map method, let the yield hidden in
        #   a generator so that the first fut will be dispatched before
        #   we start to get from fut_queue.
        return self._fut_gen(ensure_tasks_pull_interval)

    def shutdown(self, *args, **kwargs) -> None:
        """Shutdown the underlying threadpool."""
        self._executor.shutdown(*args, **kwargs)
