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


import asyncio
import threading
from queue import Queue
from typing import Dict, List, Mapping, Optional
from otaclient.app.ota_client_call import ECUNoResponse, OtaClientCall
from otaclient.app.proto import wrapper as proto_wrapper

from .ecu_status_box import ECUStatusDisplayBox


async def status_polling_thread(
    ecu_id: str,
    host: str,
    port: int,
    *,
    que: Queue,
    stop_event: threading.Event,
    poll_interval: float = 1,
):
    while not stop_event.is_set():
        try:
            resp = await OtaClientCall.status_call(
                ecu_id, host, port, request=proto_wrapper.StatusRequest()
            )
            que.put_nowait(resp)
        except ECUNoResponse:
            continue
        finally:
            await asyncio.sleep(poll_interval)


class Tracker:
    _END_SENTINEL = object()

    def __init__(self, ecu_id: str = "autoware") -> None:
        self.ecu_id = ecu_id
        self._polling_thread: Optional[threading.Thread] = None
        self._update_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._que = Queue()
        self._lock = threading.Lock()

        # display boxes for this ECU and its child ECUs
        self._ecu_status_display: Dict[str, ECUStatusDisplayBox] = {}

    def get_display_boxes(self) -> List[ECUStatusDisplayBox]:
        """NOTE: as the ecu_status_display dict might changed, always
        return a snapshot by items()."""
        with self._lock:
            return list(self._ecu_status_display.values())

    def start(self, host: str, port: int):
        def _polling_thread():
            asyncio.run(
                status_polling_thread(
                    self.ecu_id,
                    host,
                    port,
                    que=self._que,
                    stop_event=self._stop_event,
                )
            )

        def _update_thread():
            while not self._stop_event.is_set():
                _ecu_status: proto_wrapper.StatusResponse = self._que.get()
                if _ecu_status is self._END_SENTINEL:
                    return

                # get one status response, if display box for this ecu is not yet created,
                # create one for it.
                with self._lock:
                    for ecu_id in _ecu_status.available_ecu_ids:
                        if ecu_id not in self._ecu_status_display:
                            self._ecu_status_display[ecu_id] = ECUStatusDisplayBox(
                                ecu_id, len(self._ecu_status_display)
                            )

                for idx, _ecu in enumerate(_ecu_status.iter_ecu_v2()):
                    _ecu_display = self._ecu_status_display[_ecu.ecu_id]
                    _ecu_display.update_ecu_status(_ecu, index=idx)

        # start an asyncio event loop in another thread
        self._polling_thread = threading.Thread(target=_polling_thread, daemon=True)
        self._polling_thread.start()

        # start thread for updating each displaybox
        self._update_thread = threading.Thread(target=_update_thread, daemon=True)
        self._update_thread.start()

    def stop(self):
        self._stop_event.set()
        self._que.put_nowait(self._END_SENTINEL)

        if self._polling_thread and self._update_thread:
            self._polling_thread.join()
            self._update_thread.join()
