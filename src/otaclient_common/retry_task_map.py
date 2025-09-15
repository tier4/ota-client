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
from collections import OrderedDict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from queue import Empty, SimpleQueue
from typing import TYPE_CHECKING, Any, Callable, Generator, Iterable, Optional

from typing_extensions import ParamSpec

from otaclient_common._typing import RT, T
from otaclient_common.common import wait_with_backoff

P = ParamSpec("P")

logger = logging.getLogger(__name__)


class TasksEnsureFailed(Exception):
    """Exception for tasks ensuring failed."""

    @classmethod
    def caused_by(cls, *args, cause: BaseException):
        _new = cls(*args)
        _new.__cause__ = cause
        return _new

    @property
    def cause(self) -> BaseException | None:
        return self.__cause__


class WatchdogFailed(TasksEnsureFailed): ...


class _RetryOnEntryTracker:
    def __init__(self, max_entries: int) -> None:
        self._max_entries = max_entries
        self._register: OrderedDict[int, int] = OrderedDict()
        self._lock = threading.Lock()

    def register(self, entry: Any) -> int:
        with self._lock:
            _entry_id = id(entry)
            self._register[_entry_id] = _retries = self._register.get(_entry_id, 0) + 1
            self._register.move_to_end(_entry_id)
            if len(self._register) > self._max_entries:
                self._register.popitem(last=False)
            return _retries


class _ThreadPoolExecutorWithRetry(ThreadPoolExecutor):
    def __init__(
        self,
        max_concurrent: int,
        max_workers: Optional[int] = None,
        max_total_retry: Optional[int] = None,
        max_retry_on_entry: Optional[int] = None,
        thread_name_prefix: str = "",
        watchdog_func: Optional[Callable] = None,
        watchdog_check_interval: int = 3,  # seconds
        initializer: Callable[..., Any] | None = None,
        initargs: tuple = (),
        backoff_factor: float = 0.01,
        backoff_max: float = 1,
    ) -> None:
        self._rtm_start_lock = threading.Lock()
        # NOTE: ThreadPoolExecutor itself also has a _shutdown_lock!
        self._rtm_shutdown_lock = threading.Lock()
        self._rtm_started = False
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

        self._entry_retry_tracker = _RetryOnEntryTracker(max_concurrent)
        self._max_retry_on_entry = max_retry_on_entry

        self._total_retry_counter = itertools.count(start=1)
        self._concurrent_semaphore = threading.Semaphore(max_concurrent)
        self._fut_queue: SimpleQueue[Future[Any]] = SimpleQueue()
        self._exc_deque: deque[TasksEnsureFailed] = deque(maxlen=1)

        self._watchdog_check_interval = watchdog_check_interval
        self._rtm_watchdog_funcs: list[Callable[[], Any]] = []
        self._max_total_retry = max_total_retry

        if callable(watchdog_func):
            self._rtm_watchdog_funcs.append(watchdog_func)

        super().__init__(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
            initializer=initializer,
            initargs=initargs,
        )

    @property
    def _rtm_lower_pool_shutdown(self):
        return self._shutdown or self._broken or concurrent_fut_thread._shutdown

    def _watchdog_runner_thread(
        self, _checker_funcs: list[Callable[[], Any]], interval: int
    ) -> None:
        """Watchdog will shutdown the threadpool on certain conditions being met."""
        while not self._rtm_lower_pool_shutdown:
            time.sleep(interval)
            try:
                for _func in _checker_funcs:
                    _func()
            except Exception as e:
                _err_msg = f"watchdog failed: {e!r}"
                self._exc_deque.append(WatchdogFailed.caused_by(_err_msg, cause=e))
                self._rtm_shutdown(_err_msg)

    def _task_done_cb_at_thread(
        self, fut: Future[Any], /, *, item: T, func: Callable[[T], Any]
    ) -> None:
        if self._rtm_lower_pool_shutdown:
            self._concurrent_semaphore.release()  # on shutdown, always release a se
            return  # on shutdown, no need to put done fut into fut_queue
        self._fut_queue.put_nowait(fut)

        # release semaphore only on success
        _exc = fut.exception()
        if not _exc:
            self._concurrent_semaphore.release()
            return

        #
        # ------------ handling failed workitem ------------ #
        #
        # check total_retry_count
        _total_retry_count = next(self._total_retry_counter)
        if (
            self._max_total_retry is not None
            and _total_retry_count > self._max_total_retry
        ):
            _err_msg = f"{_total_retry_count=} exceeds {self._max_total_retry=}, abort! last exc: {_exc}"
            self._exc_deque.append(TasksEnsureFailed.caused_by(_err_msg, cause=_exc))
            return self._rtm_shutdown(_err_msg)

        # check continues retry on the same entry
        _retry_on_entry = self._entry_retry_tracker.register(item)
        if (
            _max_entry_retry := self._max_retry_on_entry
        ) is not None and _retry_on_entry > _max_entry_retry:
            _err_msg = f"{_retry_on_entry=} on {item=} exceed {_max_entry_retry=}, abort! last exc: {_exc}"
            self._exc_deque.append(TasksEnsureFailed.caused_by(_err_msg, cause=_exc))
            return self._rtm_shutdown(_err_msg)

        # finally, retry to re-schedule the failed workitem
        wait_with_backoff(
            _retry_on_entry,
            _backoff_factor=self.backoff_factor,
            _backoff_max=self.backoff_max,
        )
        try:
            self.submit(func, item).add_done_callback(
                partial(self._task_done_cb_at_thread, item=item, func=func)
            )
        except Exception:  # if re-schedule doesn't happen, release se
            self._concurrent_semaphore.release()

    def _fut_gen(self, interval: float) -> Generator[Future[Any]]:
        """Generator which yields the done future, controlled by the caller.

        Caller of ensure_tasks will directly cooperate with this generator.
        """
        finished_tasks = 0
        while finished_tasks == 0 or finished_tasks != self._total_task_num:
            if self._total_task_num < 0:
                return

            if self._rtm_lower_pool_shutdown:
                logger.warning(
                    f"execution interrupted: {finished_tasks=}, {self._total_task_num=}"
                )
                with contextlib.suppress(Empty):
                    while True:  # drain the _fut_queue
                        self._fut_queue.get_nowait()

                try:
                    _last_exc = self._exc_deque.pop()
                except Exception:
                    _last_exc = TasksEnsureFailed(
                        "execution interrupted due to thread pool shutdown"
                    )

                try:  # raise exc to upper caller
                    if _last_exc.cause:
                        raise _last_exc.cause
                    raise _last_exc
                finally:
                    del _last_exc

            try:
                done_fut = self._fut_queue.get_nowait()
                if not done_fut.exception():
                    finished_tasks += 1
                yield done_fut
            except Empty:
                time.sleep(interval)

    def _rtm_shutdown(self, msg: str):
        """
        Called by task_done_cb or dispatcher to shutdown threadpool on failure.
        """
        # NOTE: only one shutdown is allowed
        if self._rtm_shutdown_lock.acquire(blocking=False):
            try:
                if self._rtm_lower_pool_shutdown:
                    return  # no need to shutdown again

                _err_msg = f"shutdown executor and drain workitem queue: {msg}"
                logger.warning(_err_msg)

                # wait MUST be False to prevent deadlock when calling _on_shutdown from worker
                self.shutdown(wait=False)
                with contextlib.suppress(Empty):
                    while True:  # drain the worker queues
                        self._work_queue.get_nowait()
                # remember to add one sentinel back to the work_queue
                self._work_queue.put_nowait(None)  # type: ignore
            finally:
                self._rtm_shutdown_lock.release()

    def ensure_tasks(
        self,
        func: Callable[[T], RT],
        iterable: Iterable[T],
        *,
        ensure_tasks_pull_interval: float = 1,
    ) -> Generator[Future[RT]]:
        with self._rtm_start_lock:
            if self._rtm_started or self._rtm_lower_pool_shutdown:
                try:
                    raise TasksEnsureFailed(
                        "ensure_tasks cannot be started more than once or lower pool has already shutdown"
                    )
                finally:  # do not hold refs to input params
                    del self, func, iterable
            self._rtm_started = True

        # ------ dispatch tasks from iterable ------ #
        def _dispatcher_thread() -> None:
            _tasks_count = -1  # means no task is scheduled
            try:
                for _tasks_count, item in enumerate(iterable, start=1):
                    if self._rtm_lower_pool_shutdown:
                        return  # directly exit on shutdown

                    self._concurrent_semaphore.acquire()
                    fut = self.submit(func, item)
                    fut.add_done_callback(
                        partial(self._task_done_cb_at_thread, item=item, func=func)
                    )
            except Exception as e:
                _err_msg = f"tasks dispatcher failed: {e!r}, abort"
                self._exc_deque.append(TasksEnsureFailed.caused_by(_err_msg, cause=e))
                self._rtm_shutdown(_err_msg)
                return

            self._total_task_num = _tasks_count
            if _tasks_count > 0:
                logger.info(f"finish dispatch {_tasks_count} tasks")
            else:
                logger.warning("no task is scheduled!")

        if self._rtm_watchdog_funcs:
            threading.Thread(
                target=self._watchdog_runner_thread,
                args=(self._rtm_watchdog_funcs, self._watchdog_check_interval),
                daemon=True,
            ).start()
        threading.Thread(target=_dispatcher_thread, daemon=True).start()

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
            max_retry_on_entry: Optional[int] = None,
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
                max_retry_on_entry (Optional[int]): Max total retry on the same entry. Defaults to None.
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
            ensure_tasks_pull_interval: float = 1,
        ) -> Generator[Future[RT]]:
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

        def shutdown(self, wait: bool = False):
            raise NotImplementedError

else:
    ThreadPoolExecutorWithRetry = _ThreadPoolExecutorWithRetry
