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


import logging
import itertools
import time


class BurstSuppressFilter(logging.Filter):
    def __init__(
        self, logger_name: str, burst_max: int, burst_round_length: int
    ) -> None:
        self.name = logger_name
        self.round_length = burst_round_length
        self.burst_max = burst_max
        # for each round
        self._round_logging_count = itertools.count()
        self._round_start = 0
        self._round_reach_burst_limit = False
        self._round_warned = False

    def filter(self, _: logging.LogRecord) -> bool:
        if (cur_timestamp := int(time.time())) > self._round_start + self.round_length:
            if self._round_warned:
                logging.warning(
                    f"{next(self._round_logging_count)-1} lines of logging suppressed for logger {self.name} "
                    f"from {self._round_start} to {self._round_start+self.round_length} "
                )
            # reset logging round
            self._round_start = cur_timestamp
            self._round_reach_burst_limit = self._round_warned = False
            self._round_logging_count = itertools.count(start=2)
            return True

        if next(self._round_logging_count) <= self.burst_max:
            return True

        if not self._round_warned:
            logging.warning(
                f"logging suppressed for {self.name} until {self._round_start + self.round_length}: "
                f"exceed burst_limit={self.burst_max}"
            )
            self._round_warned = True
        return False
