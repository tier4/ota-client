import asyncio
import threading
from queue import Queue
from typing import Dict, Iterator, Optional
from otaclient.app.ota_client_call import ECUNoResponse, OtaClientCall
from otaclient.app.proto import wrapper as proto_wrapper

from .ecu_status_box import ECUStatusDisplayBox


async def status_polling_main(
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


class TrackerThread:
    _END_SENTINEL = object()

    def __init__(self) -> None:
        self.ecu_id = "autoware"
        self._polling_thread: Optional[threading.Thread] = None
        self._update_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._que = Queue()

        self.ecu_status_display: Dict[str, ECUStatusDisplayBox] = {}

    def start(self, host: str, port: int):
        def _polling_thread():
            asyncio.run(
                status_polling_main(
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
                for _ecu in _ecu_status.iter_ecu_v2():
                    _ecu_display = self.ecu_status_display[_ecu.ecu_id]
                    _ecu_display.update_ecu_status(_ecu)

        self._polling_thread = threading.Thread(target=_polling_thread, daemon=True)
        self._polling_thread.start()

        # get one status response to init the display box for each ecu
        _ecu_status: proto_wrapper.StatusResponse = self._que.get()
        for index, ecu_id in enumerate(_ecu_status.available_ecu_ids):
            self.ecu_status_display[ecu_id] = ECUStatusDisplayBox(ecu_id, index)
        self._update_thread = threading.Thread(target=_update_thread, daemon=True)
        self._update_thread.start()

    def stop(self):
        self._stop_event.set()
        self._que.put_nowait(self._END_SENTINEL)

        if self._polling_thread and self._update_thread:
            self._polling_thread.join()
            self._update_thread.join()
