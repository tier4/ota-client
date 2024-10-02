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

        super().__init__(
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

    def _watchdog(
        self,
        max_retry: int | None,
        watchdog_func: Callable[..., Any] | None,
        interval: int,
    ) -> None:
        """Watchdog will shutdown the threadpool on certain conditions being met."""
        while not self._shutdown and not concurrent_fut_thread._shutdown:
            if max_retry and self._retry_count > max_retry:
                logger.warning(f"exceed {max_retry=}, abort")
                return self.shutdown(wait=True)

            if callable(watchdog_func):
                try:
                    watchdog_func()
                except Exception as e:
                    logger.warning(f"custom watchdog func failed: {e!r}, abort")
                    return self.shutdown(wait=True)
            time.sleep(interval)

    def _task_done_cb(
        self, fut: Future[Any], /, *, item: T, func: Callable[[T], Any]
    ) -> None:
        self._concurrent_semaphore.release()  # always release se first

        if self._shutdown:
            return  # on shutdown, no need to put done fut into fut_queue
        self._fut_queue.put_nowait(fut)

        # ------ on task failed ------ #
        if fut.exception():
            self._retry_count = next(self._retry_counter)
            with contextlib.suppress(Exception):  # on threadpool shutdown
                self.submit(func, item).add_done_callback(
                    partial(self._task_done_cb, item=item, func=func)
                )

    def _fut_gen(self, interval: int) -> Generator[Future[Any], Any, None]:
        finished_tasks = 0
        while finished_tasks == 0 or finished_tasks != self._total_task_num:
            if self._total_task_num < 0:
                return

            if self._shutdown or self._broken or concurrent_fut_thread._shutdown:
                logger.warning(
                    f"failed to ensure all tasks, {finished_tasks=}, {self._total_task_num=}"
                )
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
            _tasks_count = -1  # means no task is scheduled
            try:
                for _tasks_count, item in enumerate(iterable, start=1):
                    if self._shutdown:
                        logger.warning("threadpool is closing, exit")
                        return  # directly exit on shutdown

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
