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


import itertools
import time
from typing import Optional
from otaclient.app.ota_client_call import ECUNoResponse, OtaClientCall
from otaclient.app.proto import wrapper
from . import _logutil

logger = _logutil.get_logger(__name__)


async def call_status(
    ecu_id: str,
    ecu_ip: str,
    ecu_port: int,
    *,
    interval: float = 1,
    count: Optional[int] = None,
):
    logger.debug(f"request status API on ecu(@{ecu_id}) at {ecu_ip}:{ecu_port}")
    count_iter = range(count) if count else itertools.count()

    poll_round = 0
    for poll_round in count_iter:
        logger.debug(f"status request#{poll_round}")
        try:
            response = await OtaClientCall.status_call(ecu_id, ecu_ip, ecu_port, request=wrapper.StatusRequest())
            logger.debug(f"{response.export_pb()=}")
        except ECUNoResponse as e:
            logger.debug(f"API request failed: {e!r}")
            continue
        time.sleep(interval)
