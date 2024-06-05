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
from functools import partial

import pytest

from otaclient_common.common import get_backoff
from otaclient_common.retry_task_map import RetryTaskMap, RetryTaskMapInterrupted

logger = logging.getLogger(__name__)


class _RetryTaskMapTestErr(Exception):
    """"""


class TestRetryTaskMap:
    WAIT_CONST = 100_000_000
    TASKS_COUNT = 2000
    MAX_CONCURRENT = 128
    DOWNLOAD_GROUP_NO_SUCCESS_RETRY_TIMEOUT = 6  # seconds
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

    def test_retry_keep_failing_timeout(self):
        _keep_failing_timer = time.time()
        with pytest.raises(RetryTaskMapInterrupted):
            _mapper = RetryTaskMap(
                backoff_func=partial(get_backoff, factor=0.1, _max=1),
                max_concurrent=self.MAX_CONCURRENT,
                max_retry=0,  # we are testing keep failing timeout here
            )
            for done_task in _mapper.map(
                self.workload_aways_failed, range(self.TASKS_COUNT)
            ):
                if not done_task.fut.exception():
                    # reset the failing timer on one succeeded task
                    _keep_failing_timer = time.time()
                    continue
                if (
                    time.time() - _keep_failing_timer
                    > self.DOWNLOAD_GROUP_NO_SUCCESS_RETRY_TIMEOUT
                ):
                    logger.error(
                        f"RetryTaskMap successfully failed after keep failing in {self.DOWNLOAD_GROUP_NO_SUCCESS_RETRY_TIMEOUT}s"
                    )
                    _mapper.shutdown(raise_last_exc=True)

    def test_retry_exceed_retry_limit(self):
        with pytest.raises(RetryTaskMapInterrupted):
            _mapper = RetryTaskMap(
                backoff_func=partial(get_backoff, factor=0.1, _max=1),
                max_concurrent=self.MAX_CONCURRENT,
                max_retry=3,
            )
            for _ in _mapper.map(self.workload_aways_failed, range(self.TASKS_COUNT)):
                pass

    def test_retry_finally_succeeded(self):
        _keep_failing_timer = time.time()

        _mapper = RetryTaskMap(
            backoff_func=partial(get_backoff, factor=0.1, _max=1),
            max_concurrent=self.MAX_CONCURRENT,
            max_retry=0,  # we are testing keep failing timeout here
        )
        for done_task in _mapper.map(
            self.workload_failed_and_then_succeed, range(self.TASKS_COUNT)
        ):
            # task successfully finished
            if not done_task.fut.exception():
                # reset the failing timer on one succeeded task
                _keep_failing_timer = time.time()
                continue

            if (
                time.time() - _keep_failing_timer
                > self.DOWNLOAD_GROUP_NO_SUCCESS_RETRY_TIMEOUT
            ):
                _mapper.shutdown(raise_last_exc=True)
        assert all(self._succeeded_tasks)

    def test_succeeded_in_one_try(self):
        _keep_failing_timer = time.time()
        _mapper = RetryTaskMap(
            backoff_func=partial(get_backoff, factor=0.1, _max=1),
            max_concurrent=self.MAX_CONCURRENT,
            max_retry=0,  # we are testing keep failing timeout here
        )
        for done_task in _mapper.map(self.workload_succeed, range(self.TASKS_COUNT)):
            # task successfully finished
            if not done_task.fut.exception():
                # reset the failing timer on one succeeded task
                _keep_failing_timer = time.time()
                continue

            if (
                time.time() - _keep_failing_timer
                > self.DOWNLOAD_GROUP_NO_SUCCESS_RETRY_TIMEOUT
            ):
                _mapper.shutdown(raise_last_exc=True)
        assert all(self._succeeded_tasks)
