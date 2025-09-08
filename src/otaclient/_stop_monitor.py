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
"""Implementation to monitor and execute stop requests.

The API exposed by this module is meant to be controlled by main otaclient only.
"""

from __future__ import annotations

import logging
from multiprocessing import queues as mp_queue
from queue import Empty

from otaclient._logging import configure_logging
from otaclient._types import CriticalZoneFlag, IPCRequest

STOP_REQUEST_CHECK_INTERVAL = 1  # seconds

logger = logging.getLogger(__name__)
configure_logging()


def stop_request_thread(
    *,
    otaclient_main_queue: mp_queue.Queue[IPCRequest],
    critical_zone_flag: CriticalZoneFlag,
):
    while True:
        try:
            stop_req = otaclient_main_queue.get(timeout=STOP_REQUEST_CHECK_INTERVAL)
            logger.info(f"Received stop request: {stop_req}")
            with critical_zone_flag.acquire_lock_no_release() as _lock_acquired:
                if _lock_acquired:
                    logger.info(
                        "Critical zone flag is not set. Stopping thread for shutdown."
                    )
                    return
                else:
                    logger.warning(
                        "Received stop message while in critical zone, ignoring it."
                    )
        except Empty:
            pass
