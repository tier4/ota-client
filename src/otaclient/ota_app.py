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
"""OTAClient APP main entry."""


from __future__ import annotations

import logging
import multiprocessing.synchronize as mp_sync
import threading
import time
from multiprocessing.queues import Queue as mp_Queue
from queue import Queue

from otaclient._types import (
    OTAClientStatus,
    OTAOperation,
    OTAOperationResp,
    RollbackRequestV2,
    UpdateRequestV2,
)
from otaclient.configs.proxy_info import proxy_info
from otaclient.ota_core import OTAClient
from otaclient.stats_monitor import OTAClientStatsCollector

logger = logging.getLogger(__name__)

REPORT_INTERVAL = 1


class OTAClientAPP:

    def __init__(
        self,
        *,
        status_report_queue: mp_Queue[OTAClientStatus],
        operation_push_queue: mp_Queue,
        operation_ack_queue: mp_Queue,
        reboot_flag: mp_sync.Event,
    ) -> None:
        """Main entry for otaclient process."""
        self._status_report_queue = status_report_queue
        self._operation_push_queue = operation_push_queue
        self._operation_ack_queue = operation_ack_queue

        self._last_op = None

        local_stats_collect_queue = Queue()
        self._local_otaclient_monitor = OTAClientStatsCollector(
            msg_queue=local_stats_collect_queue
        )

        self._otaclient = OTAClient(
            reboot_flag=reboot_flag,
            proxy=proxy_info.get_proxy_for_local_ota(),
            stats_report_queue=local_stats_collect_queue,
        )

    def _stats_report_thread(self) -> None:
        """Main entry for the status report thread."""
        while True:
            _status_report = self._local_otaclient_monitor.otaclient_status
            if _status_report:
                self._status_report_queue.put_nowait(_status_report)
            time.sleep(REPORT_INTERVAL)

    def _ota_operation_thread(self, request) -> None:
        """Main entry for OTA operation thread."""
        try:
            if isinstance(request, UpdateRequestV2):
                self._last_op = OTAOperation.UPDATE
                return self._otaclient.update(
                    version=request.version,
                    url_base=request.url_base,
                    cookies_json=request.cookies_json,
                )
            if isinstance(request, RollbackRequestV2):
                self._last_op = OTAOperation.ROLLBACK
                return self._otaclient.rollback()
            logger.warning(f"ignore unknown request with type {type(request)}")
        finally:
            self._last_op = None

    def start(self):
        """Main entry for OTAClient APP process."""
        # start ota_core internal stats collector thread
        self._local_otaclient_monitor.start()

        # start status report thread
        threading.Thread(
            target=self._stats_report_thread,
            name="otaclient_status_collect",
            daemon=True,
        ).start()

        while True:
            req = self._operation_push_queue.get()
            if req is None:
                break  # termination signal

            if self._last_op:
                logger.warning(f"ignore {type(req)} as {self._last_op} is ongoing")
                self._operation_ack_queue.put_nowait(OTAOperationResp.BUSY)
                continue

            threading.Thread(
                target=self._ota_operation_thread,
                args=[req],
                name="otaclient_ota_thread",
                daemon=True,
            ).start()
            self._operation_ack_queue.put_nowait(OTAOperationResp.ACCEPTED)
