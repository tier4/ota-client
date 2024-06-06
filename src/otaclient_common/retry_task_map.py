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
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from queue import Empty, SimpleQueue
from typing import Any, Callable, Generator, Iterable, Optional

from otaclient_common.typing import RT, T

logger = logging.getLogger(__name__)


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

        # NOTE: leave two threads each for watchdog and dispatcher
        max_workers = (
            max_workers + 2 if max_workers else min(32, (os.cpu_count() or 1) + 4)
        )
        super().__init__(max_workers=max_workers, thread_name_prefix=thread_name_prefix)

        def _watchdog() -> None:
            """Watchdog will shutdown the threadpool on certain conditions being met."""
            while not self._shutdown and not concurrent_fut_thread._shutdown:
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

        self.submit(_watchdog)

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
        if self._shutdown or self._broken:
            # wakeup dispatcher
            self._concurrent_semaphore.release()
            return

        # ------ on task failed ------ #
        self._retry_count = next(self._retry_counter)
        with contextlib.suppress(Exception):  # on threadpool shutdown
            self.submit(func, item).add_done_callback(
                partial(self._task_done_cb, item=item, func=func)
            )

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
            if self._started:
                raise ValueError("ensure_tasks cannot be started more than once")
            if self._shutdown or self._broken:
                raise ValueError("threadpool is shutdown or broken, abort")
            self._started = True

        # ------ dispatch tasks from iterable ------ #
        def _dispatcher() -> None:
            try:
                for _tasks_count, item in enumerate(iterable, start=1):
                    self._concurrent_semaphore.acquire()
                    fut = self.submit(func, item)
                    fut.add_done_callback(
                        partial(self._task_done_cb, item=item, func=func)
                    )
            except Exception as e:
                logger.error(f"tasks dispatcher failed: {e!r}, abort")
                self.shutdown(wait=True)
                return

            self._total_task_num = _tasks_count
            logger.info(f"finish dispatch {_tasks_count} tasks")

        self.submit(_dispatcher)

        # ------ ensure all tasks are finished ------ #
        while True:
            if self._shutdown or self._broken or concurrent_fut_thread._shutdown:
                logger.warning(
                    f"failed to ensure all tasks, {self._finished_task=}, {self._total_task_num=}"
                )
                raise TasksEnsureFailed

            try:
                yield self._fut_queue.get_nowait()
            except Empty:
                if (
                    self._total_task_num == 0
                    or self._finished_task != self._total_task_num
                ):
                    time.sleep(self.ensure_tasks_pull_interval)
                    continue
                return
