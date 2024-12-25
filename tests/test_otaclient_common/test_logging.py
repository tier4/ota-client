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


import time

from pytest import LogCaptureFixture

from otaclient_common import logging as _logging


def test_burst_logging(caplog: LogCaptureFixture):
    logger_name = "upper_logger.intermediate_logger.this_logger"
    upper_logger = "upper_logger.intermediate_logger"

    burst_round_length = 1

    logger = _logging.get_burst_suppressed_logger(
        logger_name,
        # NOTE: test upper_logger_name calculated from logger_name
        burst_max=1,
        burst_round_length=burst_round_length,
    )

    # test loggging suppressing
    # NOTE: outer loop ensures that suppression only works
    #       within each burst_round, and should be refresed
    #       in new round.
    for _ in range(2):
        for idx in range(2000):
            logger.error(idx)
        time.sleep(burst_round_length * 2)
        logger.error("burst_round end")

        # the four logging lines are:
        #   1. logger.error(idx) # idx==0
        #   2. a warning of exceeded loggings are suppressed
        #   3. a warning of how many loggings are suppressed
        #   4. logger.error("burst_round end")
        assert len(records := caplog.records) <= 4
        # warning msg comes from upper_logger
        assert records[1].name == upper_logger
        assert records[2].name == upper_logger
        caplog.clear()
