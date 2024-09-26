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

import atexit
import logging
import multiprocessing as mp
import multiprocessing.synchronize as mp_sync
import threading
import time
from queue import Empty, Queue

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

_otaclient_shutdown = False
_status_report_thread: threading.Thread | None = None
_ota_op_thread: threading.Thread | None = None


def _global_shutdown():
    global _otaclient_shutdown
    _otaclient_shutdown = True

    if _status_report_thread:
        _status_report_thread.join()
    if _ota_op_thread:
        _ota_op_thread.join()


atexit.register(_global_shutdown)

REQ_PULL_INTERVAL = 3
IDLE_REPORT_INTERVAL = 6
ACTIVE_REPORT_INTERVAL = 1


class OTAClientAPP:

    def __init__(
        self,
        *,
        status_report_queue: mp.Queue[OTAClientStatus],
        operation_queue: mp.Queue,
        reboot_flag: mp_sync.Event,
    ) -> None:
        """Main entry for otaclient process."""
        self._status_report_queue = status_report_queue
        self._operation_queue = operation_queue

        local_stats_collect_queue = Queue()
        self._local_otaclient_monitor = OTAClientStatsCollector(
            msg_queue=local_stats_collect_queue
        )

        self._otaclient = OTAClient(
            reboot_flag=reboot_flag,
            proxy=proxy_info.get_proxy_for_local_ota(),
            stats_report_queue=local_stats_collect_queue,
        )

        global _status_report_thread
        _status_report_thread = threading.Thread(
            target=self._stats_report_main,
            name="otaclient_status_collect",
            daemon=True,
        )

        self._last_op = None
        self._report_interval = IDLE_REPORT_INTERVAL

    def _stats_report_main(self) -> None:
        """Main entry for the status report thread."""
        _previous = self._local_otaclient_monitor.otaclient_status
        while not _otaclient_shutdown:
            _new = self._local_otaclient_monitor.otaclient_status
            if _new != _previous:
                _previous = _new
                self._status_report_queue.put_nowait(_new)
            time.sleep(self._report_interval)

    def _ota_operation_main(self, request) -> None:
        """Main entry for OTA operation thread."""
        try:
            if isinstance(request, UpdateRequestV2):
                self._last_op = OTAOperation.UPDATE
                self._report_interval = ACTIVE_REPORT_INTERVAL
                return self._otaclient.update(
                    version=request.version,
                    url_base=request.url_base,
                    cookies_json=request.cookies_json,
                )
            if isinstance(request, RollbackRequestV2):
                self._last_op = OTAOperation.ROLLBACK
                self._report_interval = ACTIVE_REPORT_INTERVAL
                return self._otaclient.rollback()
            logger.warning(f"ignore unknown request with type {type(request)}")
        finally:
            self._last_op = None
            self._report_interval = IDLE_REPORT_INTERVAL

    def main(self):
        """Main entry for OTAClient APP process."""
        logger.info("otaclient app started")
        while not _otaclient_shutdown:
            try:
                req = self._operation_queue.get_nowait()
            except Empty:
                time.sleep(REQ_PULL_INTERVAL)
                continue

            if self._last_op:
                logger.warning(f"ignore {type(req)} as {self._last_op} is ongoing")
                self._operation_queue.put_nowait(OTAOperationResp.BUSY)
                continue

            global _ota_op_thread
            _ota_op_thread = threading.Thread(
                target=self._ota_operation_main,
                args=[req],
                name="otaclient_ota_thread",
                daemon=True,
            )
            _ota_op_thread.start()
            self._operation_queue.put_nowait(OTAOperationResp.ACCEPTED)
