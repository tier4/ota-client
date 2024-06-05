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

import itertools
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures.thread import BrokenThreadPool
from typing import Callable, Generator, Iterable, Optional

from otaclient_common.typing import RT, T

logger = logging.getLogger(__name__)


class TasksEnsureFailed(Exception):
    pass


class ThreadPoolExecutorWithRetry(ThreadPoolExecutor):

    WATCH_DOG_CHECK_INTERVAL = 3
    ENSURE_TASKS_PULL_INTERVAL = 1

    def __init__(
        self,
        max_concurrent: int,
        max_workers: Optional[int] = None,
        thread_name_prefix: str = "",
        max_total_retry: Optional[int] = None,
    ) -> None:
        """Initialize a ThreadPoolExecutorWithRetry instance.

        Args:
            max_concurrent (int, optional): How many tasks should be kept in the memory. Defaults to 128.
            max_workers (Optional[int], optional): Max number of worker threads in the pool. Defaults to None.
            thread_name_prefix (str, optional): Defaults to "".
            max_total_retry (Optional[int], optional): Max total retry counts before abort. Defaults to None.
        """
        self.max_total_retry = max_total_retry

        self._start_lock, self._started = threading.Lock(), False
        self._finished_task_counter = itertools.count(start=1)
        self._finished_task = 0
        self._retry_counter = itertools.count(start=1)
        self._retry_count = 0
        self._concurrent_semaphore = threading.Semaphore(max_concurrent)

        if max_workers:
            max_workers += 1  # one extra thread for watchdog
        super().__init__(max_workers=max_workers, thread_name_prefix=thread_name_prefix)
        self.submit(self._watchdog)

    def _watchdog(self) -> None:
        """Watchdog watches exceeding of max_retry and max no_progress_timeout."""
        while not self._shutdown:
            if self.max_total_retry and self._retry_count > self.max_total_retry:
                logger.warning(f"exceed {self.max_total_retry=}, abort")
                return self.shutdown(wait=True)
            time.sleep(self.WATCH_DOG_CHECK_INTERVAL)

    def _task_wrapper(self, func: Callable[[T], RT]) -> Callable[[T], RT]:
        def _task(_item: T) -> RT:
            try:
                res = func(_item)
                self._finished_task = next(self._finished_task_counter)
                self._concurrent_semaphore.release()  # only release on success
                return res
            except Exception:
                self._retry_count = next(self._retry_counter)
                self.submit(_task, _item)
                raise  # still raise the exception to upper caller

        return _task

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

        task = self._task_wrapper(func)
        # ------ dispatch tasks from iterable ------ #
        for tasks_count, item in enumerate(iterable, start=1):
            try:
                with self._concurrent_semaphore:
                    yield self.submit(task, item)
            except (RuntimeError, BrokenThreadPool):
                raise TasksEnsureFailed

        # ------ ensure all tasks are finished ------ #
        while self._finished_task != tasks_count:
            if self._shutdown or self._broken:
                logger.warning(
                    f"failed to ensure all tasks, {self._finished_task=}, {tasks_count=}"
                )
                raise TasksEnsureFailed
            time.sleep(self.ENSURE_TASKS_PULL_INTERVAL)
