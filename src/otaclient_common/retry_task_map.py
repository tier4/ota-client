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
import contextlib
import itertools
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from queue import Empty, SimpleQueue
from typing import Any, Callable, Generator, Iterable, Optional

from otaclient_common.typing import RT, T

logger = logging.getLogger(__name__)

_retry_task_map_global_shutdown = False


def _python_exit():
    global _retry_task_map_global_shutdown
    _retry_task_map_global_shutdown = True


atexit.register(_python_exit)


class TasksEnsureFailed(Exception):
    """Exception for tasks ensuring failed."""


class ThreadPoolExecutorWithRetry(ThreadPoolExecutor):

    def __init__(
        self,
        max_concurrent: int,
        max_workers: Optional[int] = None,
        max_total_retry: Optional[int] = None,
        thread_name_prefix: str = "",
        watchdog_func: Optional[Callable] = None,
        watchdog_check_interval: int = 3,  # seconds
        ensure_tasks_pull_interval: int = 1,  # second
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
            ensure_tasks_pull_interval (int): Defaults to 1(second).
        """
        self.max_total_retry = max_total_retry
        self.ensure_tasks_pull_interval = ensure_tasks_pull_interval

        self._start_lock, self._started = threading.Lock(), False
        self._total_task_num = 0
        self._finished_task_counter = itertools.count(start=1)
        self._finished_task = 0
        self._retry_counter = itertools.count(start=1)
        self._retry_count = 0
        self._concurrent_semaphore = threading.Semaphore(max_concurrent)
        self._fut_queue: SimpleQueue[Future[Any]] = SimpleQueue()

        super().__init__(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
            initializer=initializer,
            initargs=initargs,
        )

        threading.Thread(
            target=self._watchdog,
            args=(watchdog_func, watchdog_check_interval),
            daemon=True,
        ).start()

    def _watchdog(
        self, watchdog_func: Callable[..., Any], watchdog_check_interval: int
    ) -> None:
        """Watchdog will shutdown the threadpool on certain conditions being met."""
        if not self.max_total_retry and not callable(watchdog_func):
            return  # no need to run watchdog thread if not checks are performed

        while not self._shutdown and not _retry_task_map_global_shutdown:
            if self.max_total_retry and self._retry_count > self.max_total_retry:
                logger.warning(f"exceed {self.max_total_retry=}, abort")
                return self.shutdown(wait=True)

            if callable(watchdog_func):
                try:
                    watchdog_func()
                except Exception as e:
                    logger.warning(f"custom watchdog func failed: {e!r}, abort")
                    return self.shutdown(wait=True)
            time.sleep(watchdog_check_interval)

    def _task_done_cb(
        self, fut: Future[Any], /, *, item: T, func: Callable[[T], Any]
    ) -> None:
        self._fut_queue.put_nowait(fut)

        # ------ on task succeeded ------ #
        if not fut.exception():
            self._concurrent_semaphore.release()
            self._finished_task = next(self._finished_task_counter)
            return

        # ------ on threadpool shutdown(by watchdog) ------ #
        if self._shutdown or _retry_task_map_global_shutdown:
            self._concurrent_semaphore.release()  # wakeup dispatcher
            return

        # ------ on task failed ------ #
        self._retry_count = next(self._retry_counter)
        with contextlib.suppress(Exception):  # on threadpool shutdown
            self.submit(func, item).add_done_callback(
                partial(self._task_done_cb, item=item, func=func)
            )

    def _dispatcher(self, func: Callable[[T], RT], iterable: Iterable[T]) -> None:
        try:
            for _tasks_count, item in enumerate(iterable, start=1):
                if self._shutdown or _retry_task_map_global_shutdown:
                    return

                self._concurrent_semaphore.acquire()
                fut = self.submit(func, item)
                fut.add_done_callback(partial(self._task_done_cb, item=item, func=func))
        except Exception as e:
            logger.error(f"tasks dispatcher failed: {e!r}, abort")
            self.shutdown(wait=True)
            return

        self._total_task_num = _tasks_count
        logger.info(f"finish dispatch {_tasks_count} tasks")

    def ensure_tasks(
        self, func: Callable[[T], RT], iterable: Iterable[T]
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
            if self._started or self._shutdown or _retry_task_map_global_shutdown:
                try:
                    raise ValueError(
                        "pool shutdowned or ensure_tasks cannot be started more than once"
                    )
                finally:  # do not hold refs to input params
                    del self, func, iterable
            self._started = True

        # ------ dispatch tasks from iterable ------ #
        threading.Thread(
            target=self._dispatcher, args=(func, iterable), daemon=True
        ).start()

        # ------ ensure all tasks are finished ------ #
        while self._total_task_num == 0 or self._finished_task != self._total_task_num:
            # shutdown by upper caller or interpreter exits
            if self._shutdown or _retry_task_map_global_shutdown:
                _err_msg = f"failed to ensure all tasks, {self._finished_task=}, {self._total_task_num=}"
                logger.warning(_err_msg)

                try:
                    raise TasksEnsureFailed(_err_msg)
                finally:
                    del self, func, iterable

            try:
                yield self._fut_queue.get_nowait()
            except Empty:
                time.sleep(self.ensure_tasks_pull_interval)
