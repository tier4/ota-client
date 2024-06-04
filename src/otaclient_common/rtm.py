from __future__ import annotations

import itertools
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures.thread import BrokenThreadPool
from typing import Callable, Generator, Iterable, Optional

from otaclient_common.typing import T, RT

logger = logging.getLogger(__name__)


class ThreadPoolExecutorWithRetry(ThreadPoolExecutor):

    WATCH_DOG_CHECK_INTERVAL = 3
    ENSURE_TASKS_PULL_INTERVAL = 1

    def __init__(
        self,
        max_concurrent: int = 128,
        max_workers: Optional[int] = None,
        thread_name_prefix: str = "",
        max_total_retry: Optional[int] = None,
        no_progress_timeout: Optional[int] = None,
    ) -> None:
        self.max_total_retry = max_total_retry
        self.no_progress_timeout = no_progress_timeout

        self._start_lock, self._started = threading.Lock(), False
        self._finished_task_counter = itertools.count(start=1)
        self._finished_task = 0
        self._retry_counter = itertools.count(start=1)
        self._retry_count = 0
        self._last_success_timestamp = int(time.time())
        self._concurrent_semaphore = threading.Semaphore(max_concurrent)
        self._executor_interrupted = False

        if max_workers:
            max_workers += 1  # one extra thread for watchdog
        super().__init__(max_workers=max_workers, thread_name_prefix=thread_name_prefix)
        self.submit(self._watchdog)

    def _watchdog(self) -> None:
        if self.no_progress_timeout is None:
            return

        while not self._shutdown:
            if (
                int(time.time()) - self._last_success_timestamp
                > self.no_progress_timeout
            ):
                logger.warning(f"exceed {self.no_progress_timeout=}, abort")
                self._executor_interrupted = True
                return self.shutdown(wait=True)
            if self.executor_interrupted:
                logger.warning("execution is interrupted, abort")
                return self.shutdown(wait=True)
            time.sleep(self.WATCH_DOG_CHECK_INTERVAL)

    def _task_wrapper(self, func: Callable[[T], RT]) -> Callable[[T], RT]:
        def _task(_item: T) -> RT:
            try:
                res = func(_item)
                self._last_success_timestamp = int(time.time())
                self._finished_task = next(self._finished_task_counter)
                return res
            except Exception:
                self._retry_count = next(self._retry_counter)
                self.submit(_task, _item)
                raise  # still raise the exception to upper caller
            finally:
                self._concurrent_semaphore.release()

        return _task

    # APIs

    @property
    def executor_interrupted(self) -> bool:
        return self._executor_interrupted

    def ensure_tasks(
        self, func: Callable[[T], RT], iterable: Iterable[T]
    ) -> Generator[Future[RT], None, None]:
        _pool_closed_err_msg = "threadpool is broken or shutdown, abort"
        with self._start_lock:
            if self._started:
                raise ValueError("ensure_tasks cannot be started more than once")
            if self._shutdown or self._broken:
                raise ValueError(_pool_closed_err_msg)
            self._started = True

        task = self._task_wrapper(func)

        # ------ dispatch tasks from iterable ------ #
        for tasks_count, item in enumerate(iterable, start=1):
            try:
                with self._concurrent_semaphore:
                    yield self.submit(task, item)
            except (RuntimeError, BrokenThreadPool):
                self._executor_interrupted = True
                logger.warning(_pool_closed_err_msg)
                return

        # ------ ensure all tasks are finished ------ #
        while self._finished_task != tasks_count:
            if self._shutdown or self._broken:
                logger.warning(_pool_closed_err_msg)
                self._executor_interrupted = True
                return

            if self.max_total_retry and self._retry_count > self.max_total_retry:
                logger.warning(f"exceed {self.max_total_retry=}, abort")
                self._executor_interrupted = True
                return
            time.sleep(self.ENSURE_TASKS_PULL_INTERVAL)
