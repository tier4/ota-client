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
import time

import pytest

from otaclient_common import retry_task_map

logger = logging.getLogger(__name__)


class _RetryTaskMapTestErr(Exception):
    """"""


class TestRetryTaskMap:
    WAIT_CONST = 100_000_000
    TASKS_COUNT = 2000
    MAX_CONCURRENT = 128
    MAX_WAIT_BEFORE_SUCCESS = 10

    @pytest.fixture(autouse=True)
    def setup(self):
        self._start_time = time.time()
        self._success_wait_dict = {
            idx: random.randint(0, self.MAX_WAIT_BEFORE_SUCCESS)
            for idx in range(self.TASKS_COUNT)
        }
        self._succeeded_tasks = [False for _ in range(self.TASKS_COUNT)]

    def workload_aways_failed(self, idx: int) -> int:
        time.sleep((self.TASKS_COUNT - random.randint(0, idx)) / self.WAIT_CONST)
        raise _RetryTaskMapTestErr

    def workload_failed_and_then_succeed(self, idx: int) -> int:
        time.sleep((self.TASKS_COUNT - random.randint(0, idx)) / self.WAIT_CONST)
        if time.time() > self._start_time + self._success_wait_dict[idx]:
            self._succeeded_tasks[idx] = True
            return idx
        raise _RetryTaskMapTestErr

    def workload_succeed(self, idx: int) -> int:
        time.sleep((self.TASKS_COUNT - random.randint(0, idx)) / self.WAIT_CONST)
        self._succeeded_tasks[idx] = True
        return idx

    def test_watchdog_breakout(self):
        MAX_RETRY, failure_count = 200, 0

        def _exit_on_exceed_max_count():
            if failure_count > MAX_RETRY:
                raise ValueError(f"{failure_count=} > {MAX_RETRY=}")

        with retry_task_map.ThreadPoolExecutorWithRetry(
            max_concurrent=self.MAX_CONCURRENT,
            watchdog_func=_exit_on_exceed_max_count,
        ) as executor:
            with pytest.raises(retry_task_map.TasksEnsureFailed):
                for _fut in executor.ensure_tasks(
                    self.workload_aways_failed, range(self.TASKS_COUNT)
                ):
                    if _fut.exception():
                        failure_count += 1

    def test_retry_exceed_retry_limit(self):
        with retry_task_map.ThreadPoolExecutorWithRetry(
            max_concurrent=self.MAX_CONCURRENT, max_total_retry=200
        ) as executor:
            with pytest.raises(retry_task_map.TasksEnsureFailed):
                for _ in executor.ensure_tasks(
                    self.workload_aways_failed, range(self.TASKS_COUNT)
                ):
                    pass

    def test_retry_finally_succeeded(self):
        count = 0
        with retry_task_map.ThreadPoolExecutorWithRetry(
            max_concurrent=self.MAX_CONCURRENT
        ) as executor:
            for _fut in executor.ensure_tasks(
                self.workload_failed_and_then_succeed, range(self.TASKS_COUNT)
            ):
                if not _fut.exception():
                    count += 1
        assert all(self._succeeded_tasks)
        assert self.TASKS_COUNT == count

    def test_succeeded_in_one_try(self):
        count = 0
        with retry_task_map.ThreadPoolExecutorWithRetry(
            max_concurrent=self.MAX_CONCURRENT
        ) as executor:
            for _fut in executor.ensure_tasks(
                self.workload_succeed, range(self.TASKS_COUNT)
            ):
                if not _fut.exception():
                    count += 1
        assert all(self._succeeded_tasks)
        assert self.TASKS_COUNT == count
