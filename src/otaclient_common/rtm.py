from __future__ import annotations

import itertools
import logging
import time
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from queue import Empty, SimpleQueue
from typing import Any, Callable, Generator, Iterable, Optional

from otaclient_common.typing import T, RT

logger = logging.getLogger(__name__)


class ThreadPoolExecutorWithRetry:

    WATCH_DOG_CHECK_INTERVAL = 3
    ENSURE_TASKS_PULL_INTERVAL = 1

    def __init__(
        self,
        max_workers: Optional[int] = None,
        thread_name_prefix: str = "",
        max_total_retry: Optional[int] = None,
        no_progress_timeout: Optional[int] = None,
    ) -> None:
        self.max_total_retry = max_total_retry
        self.no_progress_timeout = no_progress_timeout

        self._queue: SimpleQueue[Any] = SimpleQueue()
        self._finished_task_counter = itertools.count(start=1)
        self._finished_task = 0
        self._last_success_timestamp = 0

        self._lock = threading.Lock()
        self._executor_interrupted = False
        self._started = False
        self._shutdown = False

        self._executer = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix
        )
        _watchdog = threading.Thread(target=self._watchdog, daemon=True)
        _watchdog.start()

    def _watchdog(self) -> None:
        if self.no_progress_timeout is None:
            return

        while not (self._shutdown or self._executer._shutdown):
            if (
                self._last_success_timestamp > 0
                and int(time.time()) - self._last_success_timestamp
                > self.no_progress_timeout
            ):
                logger.warning(
                    (
                        f"the threadpool keeps inactive longer than {self.no_progress_timeout}s, "
                        "shutdown threadpool..."
                    )
                )
                self._executer.shutdown(wait=True)
                self._executor_interrupted = True
                return
            time.sleep(self.WATCH_DOG_CHECK_INTERVAL)

    def _task_wrapper(self, func: Callable[[T], RT]) -> Callable[[T], RT]:
        def _task(_item: T) -> RT:
            try:
                res = func(_item)
                self._last_success_timestamp = int(time.time())
                self._finished_task = next(self._finished_task_counter)
                return res
            except Exception:
                self._queue.put_nowait(_item)
                raise  # still raise the exception to upper caller

        return _task

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._shutdown = True
        self._executer.shutdown(wait=True)
        return False

    # APIs

    @property
    def executor_interrupted(self) -> bool:
        return self._executor_interrupted

    def ensure_tasks(
        self, func: Callable[[T], RT], iterable: Iterable[T]
    ) -> Generator[Future[RT], None, None]:
        with self._lock:
            if self._started or self._shutdown:
                self._shutdown = True
                raise ValueError("ensure_tasks cannot be called more than once")
            self._started = True

        task = self._task_wrapper(func)
        # ------ dispatch tasks from iterable ------ #
        for tasks_count, item in enumerate(iterable, start=1):
            yield self._executer.submit(task, item)

        # ------ ensure all tasks are finished ------ #
        retry_count = 0
        while self._finished_task != tasks_count:
            try:
                item = self._queue.get_nowait()
            except Empty:
                time.sleep(self.ENSURE_TASKS_PULL_INTERVAL)
                continue

            retry_count += 1
            if self.max_total_retry and retry_count > self.max_total_retry:
                self._executor_interrupted = True
                return self._executer.shutdown(wait=True)
            yield self._executer.submit(task, item)
