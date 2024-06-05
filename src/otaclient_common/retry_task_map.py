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
from functools import partial
from queue import Queue
from typing import (
    Any,
    Callable,
    Generator,
    Generic,
    Iterable,
    NamedTuple,
    Optional,
    Set,
    TypeVar,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


class DoneTask(NamedTuple):
    fut: Future
    entry: Any


class RetryTaskMapInterrupted(Exception):
    pass


class _TaskMap(Generic[T]):
    def __init__(
        self,
        executor: ThreadPoolExecutor,
        max_concurrent: int,
        backoff_func: Callable[[int], float],
    ) -> None:
        # task dispatch interval for continues failling
        self.started = False  # can only be started once
        self._backoff_func = backoff_func
        self._executor = executor
        self._shutdown_event = threading.Event()
        self._se = threading.Semaphore(max_concurrent)

        self._total_tasks_count = 0
        self._dispatched_tasks: Set[Future] = set()
        self._failed_tasks: Set[T] = set()
        self._last_failed_fut: Optional[Future] = None

        # NOTE: itertools.count is only thread-safe in CPython with GIL,
        #       as itertools.count is pure C implemented, calling next over
        #       it is atomic in Python level.
        self._done_task_counter = itertools.count(start=1)
        self._all_done = threading.Event()
        self._dispatch_done = False

        self._done_que: Queue[DoneTask] = Queue()

    def _done_task_cb(self, item: T, fut: Future):
        """
        Tracking done counting, set all_done event.
        add failed to failed list.
        """
        self._se.release()  # always release se first
        # NOTE: don't change dispatched_tasks if shutdown_event is set
        if self._shutdown_event.is_set():
            return

        self._dispatched_tasks.discard(fut)
        # check if we finish all tasks
        _done_task_num = next(self._done_task_counter)
        if self._dispatch_done and _done_task_num == self._total_tasks_count:
            logger.debug("all done!")
            self._all_done.set()

        if fut.exception():
            self._failed_tasks.add(item)
            self._last_failed_fut = fut
        self._done_que.put_nowait(DoneTask(fut, item))

    def _task_dispatcher(self, func: Callable[[T], Any], _iter: Iterable[T]):
        """A dispatcher in a dedicated thread that dispatches
        tasks to threadpool."""
        for item in _iter:
            if self._shutdown_event.is_set():
                return
            self._se.acquire()
            self._total_tasks_count += 1

            fut = self._executor.submit(func, item)
            fut.add_done_callback(partial(self._done_task_cb, item))
            self._dispatched_tasks.add(fut)
        logger.debug(f"dispatcher done: {self._total_tasks_count=}")
        self._dispatch_done = True

    def _done_task_collector(self) -> Generator[DoneTask, None, None]:
        """A generator for caller to yield done task from."""
        _count = 0
        while not self._shutdown_event.is_set():
            if self._all_done.is_set() and _count == self._total_tasks_count:
                logger.debug("collector done!")
                return

            yield self._done_que.get()
            _count += 1

    def map(self, func: Callable[[T], Any], _iter: Iterable[T]):
        if self.started:
            raise ValueError(f"{self.__class__} inst can only be started once")
        self.started = True

        self._task_dispatcher_fut = self._executor.submit(
            self._task_dispatcher, func, _iter
        )
        self._task_collector_gen = self._done_task_collector()
        return self._task_collector_gen

    def shutdown(self, *, raise_last_exc=False) -> Optional[Set[T]]:
        """Set the shutdown event, and cancal/cleanup ongoing tasks."""
        if not self.started or self._shutdown_event.is_set():
            return

        self._shutdown_event.set()
        self._task_collector_gen.close()
        # wait for dispatch to stop
        self._task_dispatcher_fut.result()

        # cancel all the dispatched tasks
        for fut in self._dispatched_tasks:
            fut.cancel()
        self._dispatched_tasks.clear()

        if not self._failed_tasks:
            return
        try:
            if self._last_failed_fut:
                _exc = self._last_failed_fut.exception()
                _err_msg = f"{len(self._failed_tasks)=}, last failed: {_exc!r}"
                if raise_last_exc:
                    raise RetryTaskMapInterrupted(_err_msg) from _exc
                else:
                    logger.warning(_err_msg)
            return self._failed_tasks.copy()
        finally:
            # be careful not to create ref cycle here
            self._failed_tasks.clear()
            _exc, self = None, None


class RetryTaskMap(Generic[T]):
    def __init__(
        self,
        *,
        backoff_func: Callable[[int], float],
        max_retry: int,
        max_concurrent: int,
        max_workers: Optional[int] = None,
    ) -> None:
        self._running_inst: Optional[_TaskMap] = None
        self._map_gen: Optional[Generator] = None

        self._backoff_func = backoff_func
        self._retry_counter = range(max_retry) if max_retry else itertools.count()
        self._max_concurrent = max_concurrent
        self._max_workers = max_workers
        self._executor = ThreadPoolExecutor(max_workers=self._max_workers)

    def map(
        self, _func: Callable[[T], Any], _iter: Iterable[T]
    ) -> Generator[DoneTask, None, None]:
        retry_round = 0
        for retry_round in self._retry_counter:
            self._running_inst = _inst = _TaskMap(
                self._executor, self._max_concurrent, self._backoff_func
            )
            logger.debug(f"{retry_round=} started")

            yield from _inst.map(_func, _iter)

            # this retry round ends, check overall result
            if _failed_list := _inst.shutdown(raise_last_exc=False):
                _iter = _failed_list  # feed failed to next round
                # deref before entering sleep
                self._running_inst, _inst = None, None

                logger.warning(f"retry#{retry_round+1}: retry on {len(_failed_list)=}")
                time.sleep(self._backoff_func(retry_round))
            else:  # all tasks finished successfully
                self._running_inst, _inst = None, None
                return
        try:
            raise RetryTaskMapInterrupted(f"exceed try limit: {retry_round}")
        finally:
            # cleanup the defs
            _func, _iter = None, None  # type: ignore

    def shutdown(self, *, raise_last_exc: bool):
        try:
            logger.debug("shutdown retry task map")
            if self._running_inst:
                self._running_inst.shutdown(raise_last_exc=raise_last_exc)
            # NOTE: passthrough the exception from underlying running_inst
        finally:
            self._running_inst = None
            self._executor.shutdown(wait=True)
