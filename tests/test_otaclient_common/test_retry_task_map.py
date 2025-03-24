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

import logging
import random
import threading
import time
from collections import defaultdict

import pytest

from otaclient_common import retry_task_map

logger = logging.getLogger(__name__)

# ------ test setup ------ #
WAIT_CONST = 100_000_000
TASKS_COUNT = 2000
MAX_CONCURRENT = 128
MAX_WAIT_BEFORE_SUCCESS = 10
THREAD_INIT_MSG = "thread init message"
BACKOFF_FACTOR = 0.001  # for faster test
BACKOFF_MAX = 0.1


class _RetryTaskMapTestErr(Exception):
    def __init__(self, idx: int, *args: object) -> None:
        self.idx = idx
        super().__init__(*args)


def _thread_initializer(msg: str) -> None:
    """For testing thread worker initializer."""
    thread_native_id = threading.get_native_id()
    logger.info(f"thread worker #{thread_native_id} initialized: {msg}")


class TestRetryTaskMap:

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        self._start_time = time.time()
        self._success_wait_dict = {
            idx: random.randint(0, MAX_WAIT_BEFORE_SUCCESS)
            for idx in range(TASKS_COUNT)
        }
        self._succeeded_tasks = [False for _ in range(TASKS_COUNT)]

    def workload_aways_failed(self, idx: int) -> int:
        time.sleep((TASKS_COUNT - random.randint(0, idx)) / WAIT_CONST)
        raise _RetryTaskMapTestErr(idx)

    def workload_failed_and_then_succeed(self, idx: int) -> int:
        time.sleep((TASKS_COUNT - random.randint(0, idx)) / WAIT_CONST)
        if time.time() > self._start_time + self._success_wait_dict[idx]:
            self._succeeded_tasks[idx] = True
            return idx
        raise _RetryTaskMapTestErr(idx)

    def workload_succeed(self, idx: int) -> int:
        time.sleep((TASKS_COUNT - random.randint(0, idx)) / WAIT_CONST)
        self._succeeded_tasks[idx] = True
        return idx

    def test_watchdog_breakout(self):
        MAX_RETRY, failure_count = 200, 0

        def _exit_on_exceed_max_count():
            if failure_count > MAX_RETRY:
                raise ValueError(f"{failure_count=} > {MAX_RETRY=}")

        with retry_task_map.ThreadPoolExecutorWithRetry(
            max_concurrent=MAX_CONCURRENT,
            watchdog_func=_exit_on_exceed_max_count,
            initializer=_thread_initializer,
            initargs=(THREAD_INIT_MSG,),
            backoff_factor=BACKOFF_FACTOR,
            backoff_max=BACKOFF_MAX,
        ) as executor:
            with pytest.raises(retry_task_map.TasksEnsureFailed):
                for _fut in executor.ensure_tasks(
                    self.workload_aways_failed, range(TASKS_COUNT)
                ):
                    if _fut.exception():
                        failure_count += 1

        assert failure_count >= MAX_RETRY

    def test_retry_exceed_total_retry_limit(self):
        MAX_TOTAL_RETRY = 200
        failure_count = 0
        with retry_task_map.ThreadPoolExecutorWithRetry(
            max_concurrent=MAX_CONCURRENT,
            max_total_retry=MAX_TOTAL_RETRY,
            initializer=_thread_initializer,
            initargs=(THREAD_INIT_MSG,),
            backoff_factor=BACKOFF_FACTOR,
            backoff_max=BACKOFF_MAX,
        ) as executor:
            with pytest.raises(retry_task_map.TasksEnsureFailed):
                for _fut in executor.ensure_tasks(
                    self.workload_aways_failed,
                    range(TASKS_COUNT),
                    ensure_tasks_pull_interval=0.001,
                ):
                    if _fut.exception():
                        failure_count += 1

        assert failure_count >= MAX_TOTAL_RETRY

    def test_retry_exceed_entry_retry_limit(self):
        MAX_RETRY_ON_ENTRY = 30
        entry_failure_count: defaultdict[int, int] = defaultdict(lambda: 0)
        with retry_task_map.ThreadPoolExecutorWithRetry(
            max_concurrent=16,
            max_retry_on_entry=MAX_RETRY_ON_ENTRY,
            initializer=_thread_initializer,
            initargs=(THREAD_INIT_MSG,),
            backoff_factor=BACKOFF_FACTOR,
            backoff_max=BACKOFF_MAX,
        ) as executor:
            with pytest.raises(retry_task_map.TasksEnsureFailed):
                for _fut in executor.ensure_tasks(
                    self.workload_aways_failed,
                    range(TASKS_COUNT),
                    # need to be faster enough, otherwise fut will come later than pool shutdown
                    ensure_tasks_pull_interval=0.0001,
                ):
                    if _exc := _fut.exception():
                        assert isinstance(_exc, _RetryTaskMapTestErr)
                        entry_failure_count[_exc.idx] += 1

        assert any(
            _failure_count > MAX_RETRY_ON_ENTRY
            for _failure_count in entry_failure_count.values()
        )

    def test_retry_finally_succeeded(self):
        count = 0
        with retry_task_map.ThreadPoolExecutorWithRetry(
            max_concurrent=MAX_CONCURRENT,
            initializer=_thread_initializer,
            initargs=(THREAD_INIT_MSG,),
            backoff_factor=BACKOFF_FACTOR,
            backoff_max=BACKOFF_MAX,
        ) as executor:
            for _fut in executor.ensure_tasks(
                self.workload_failed_and_then_succeed, range(TASKS_COUNT)
            ):
                if not _fut.exception():
                    count += 1
        assert all(self._succeeded_tasks)
        assert TASKS_COUNT == count

    def test_succeeded_in_one_try(self):
        count = 0
        with retry_task_map.ThreadPoolExecutorWithRetry(
            max_concurrent=MAX_CONCURRENT,
            initializer=_thread_initializer,
            initargs=(THREAD_INIT_MSG,),
            backoff_factor=BACKOFF_FACTOR,
            backoff_max=BACKOFF_MAX,
        ) as executor:
            for _fut in executor.ensure_tasks(
                self.workload_succeed, range(TASKS_COUNT)
            ):
                if not _fut.exception():
                    count += 1
        assert all(self._succeeded_tasks)
        assert TASKS_COUNT == count


def test_input_yield_no_task():
    count = 0
    with retry_task_map.ThreadPoolExecutorWithRetry(
        max_concurrent=MAX_CONCURRENT,
    ) as executor:
        for _ in executor.ensure_tasks(
            func=lambda _: _,
            iterable=[],
        ):
            count += 1
    assert count == 0
