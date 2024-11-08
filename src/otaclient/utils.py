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
"""Common shared utils, only used by otaclient package."""


from __future__ import annotations

import itertools
import logging
import time
from abc import abstractmethod
from typing import Callable, Protocol

logger = logging.getLogger(__name__)


class CheckableFlag(Protocol):

    @abstractmethod
    def is_set(self) -> bool: ...


def wait_and_log(
    flag: CheckableFlag,
    message: str = "",
    *,
    check_interval: int = 2,
    log_interval: int = 30,
    log_func: Callable[[str], None] = logger.info,
) -> None:
    """Wait for <flag> until it is set while print a log every <log_interval>."""
    log_round = 0
    for seconds in itertools.count(step=check_interval):
        if flag.is_set():
            return

        _new_log_round = seconds // log_interval
        if _new_log_round > log_round:
            log_func(f"wait for {message}: {seconds}s passed ...")
            log_round = _new_log_round
        time.sleep(check_interval)
