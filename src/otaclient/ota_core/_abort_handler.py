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
import os
import signal
import sys
import threading
from queue import Empty, Queue
from typing import TYPE_CHECKING

from otaclient import errors as ota_errors
from otaclient._status_monitor import (
    OTAStatusChangeReport,
    StatusReport,
)
from otaclient._types import (
    AbortRequestV2,
    AbortState,
    IPCResEnum,
    IPCResponse,
    OTAStatus,
)

if TYPE_CHECKING:
    from ._main import OTAClient

logger = logging.getLogger(__name__)

ABORT_SIGNAL = signal.SIGUSR1
EXIT_CODE_OTA_ABORTED = 79


def _abort_signal_handler(signum: int, frame: object) -> None:
    sys.exit(EXIT_CODE_OTA_ABORTED)


class AbortHandler:
    """Daemon thread that processes abort requests, performs cleanup, and
    terminates the OTA Core process via custom signal.

    Created once by OTAClient.main() and runs for the lifetime of the
    OTA Core process.  Session-specific fields (boot_controller, etc.)
    are set by OTAClient.update() before the update begins.

    The main loop receives AbortRequestV2 from the shared IPC queue and
    forwards it to this handler via internal threading queues.  The main
    loop then blocks on get_response() and relays the response back to
    the shared IPC response queue.

    Checks _live_ota_status via the OTAClient reference to reject aborts
    when no update is active.

    During critical zone, abort is queued (REQUESTED).  When the updater
    calls exit_critical_zone(), it transitions REQUESTED → ABORTING and
    raises OTAAbortSignal.  The abort handler thread detects ABORTING
    and runs _perform_abort().

    During final phase, abort is rejected.
    """

    def __init__(
        self,
        *,
        ota_client: OTAClient,
    ) -> None:
        self._thread = None
        self._ota_client = ota_client
        self._abort_queue: Queue[AbortRequestV2] = Queue()
        self._resp_queue: Queue[IPCResponse] = Queue()
        self._state = AbortState.NONE
        self._cond = threading.Condition()

        # Per-session field, set by update() before the update starts.
        self._session_id: str = ""

    @property
    def state(self) -> AbortState:
        with self._cond:
            return self._state

    def set_session(self, *, session_id: str) -> None:
        """Set per-session fields and reset state for the current update."""
        with self._cond:
            self._session_id = session_id
            logger.info(
                f"abort handler: {self._state} -> {AbortState.NONE} (session reset, {session_id=})"
            )
            self._state = AbortState.NONE

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="abort_handler",
        )
        self._thread.start()

    def _perform_abort(self) -> None:
        """Perform abort and kill the ota_core process.

        Always called on the abort handler thread with state already set
        to ABORTING.  In Path A (immediate), called directly from _handle().
        In Path B (queued), called after _run() detects ABORTING.

        Session workdir cleanup is not done here because SIGUSR1 terminates
        the process, and on restart OTAClient.__init__ unmounts the parent
        tmpfs (_update_session_dir), which wipes all session subdirectories.
        """
        self._ota_client.report_status(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=OTAStatus.ABORTING,
                ),
                session_id=self._session_id,
            )
        )

        logger.info("Performing abort...")
        self._ota_client.boot_controller.on_abort()
        with self._cond:
            logger.info(
                f"abort handler: {self._state} -> {AbortState.ABORTED}"
            )
            self._state = AbortState.ABORTED

        logger.info(f"Sending {ABORT_SIGNAL.name} to terminate OTA Core process")
        os.kill(os.getpid(), ABORT_SIGNAL)

    # --- Zone transition methods (called by updater thread) ---

    def enter_critical_zone(self) -> None:
        """NONE → CRITICAL_ZONE.

        Raises OTAAbortSignal if abort is in progress.
        """
        with self._cond:
            if self._state in (AbortState.ABORTING, AbortState.ABORTED):
                raise ota_errors.OTAAbortSignal(
                    "cannot enter critical zone: abort in progress",
                    module=__name__,
                )
            logger.info(
                f"abort handler: {self._state} -> {AbortState.CRITICAL_ZONE}"
            )
            self._state = AbortState.CRITICAL_ZONE

    def exit_critical_zone(self) -> None:
        """CRITICAL_ZONE → NONE (normal).

        REQUESTED → ABORTING → raise OTAAbortSignal.
        The abort handler thread detects ABORTING and runs _perform_abort().
        """
        with self._cond:
            if self._state == AbortState.REQUESTED:
                logger.info(
                    f"abort handler: {self._state} -> {AbortState.ABORTING} (deferred abort)"
                )
                self._state = AbortState.ABORTING
                self._cond.notify()
            elif self._state in (AbortState.ABORTING, AbortState.ABORTED):
                raise ota_errors.OTAAbortSignal(
                    "abort in progress",
                    module=__name__,
                )
            else:
                logger.info(
                    f"abort handler: {self._state} -> {AbortState.NONE} (exiting critical zone)"
                )
                self._state = AbortState.NONE
                return

        # REQUESTED → ABORTING path: abort handler thread will do cleanup.
        raise ota_errors.OTAAbortSignal(
            "abort queued during critical zone, now executing",
            module=__name__,
        )

    def enter_final_phase(self) -> None:
        """NONE → FINAL_PHASE.

        Raises OTAAbortSignal if abort is in progress.
        """
        with self._cond:
            if self._state in (AbortState.ABORTING, AbortState.ABORTED):
                raise ota_errors.OTAAbortSignal(
                    "cannot enter final phase: abort in progress",
                    module=__name__,
                )
            logger.info(
                f"abort handler: {self._state} -> {AbortState.FINAL_PHASE}"
            )
            self._state = AbortState.FINAL_PHASE

    def submit(self, request: AbortRequestV2) -> None:
        """Submit an abort request to the handler thread."""
        self._abort_queue.put_nowait(request)

    def get_response(self, timeout: float = 3) -> IPCResponse | None:
        """Block until the handler thread produces a response.

        Returns None if no response arrives within *timeout* seconds
        (e.g. abort handler thread crashed).
        """
        try:
            return self._resp_queue.get(timeout=timeout)
        except Empty:
            logger.error(f"abort handler did not respond within {timeout}s")
            return None

    # --- Abort request handling (abort handler thread) ---

    def _run(self) -> None:
        while True:
            request = self._abort_queue.get()
            self._handle(request)

            # If abort was queued during critical zone, wait for
            # exit_critical_zone() to transition REQUESTED → ABORTING.
            with self._cond:
                while self._state == AbortState.REQUESTED:
                    self._cond.wait()
                if self._state == AbortState.ABORTING:
                    self._perform_abort()

    def _handle(self, request: AbortRequestV2) -> None:
        with self._cond:
            if self._ota_client.live_ota_status != OTAStatus.UPDATING:
                logger.info("abort handler: rejected, no active OTA update in progress")
                self._resp_queue.put_nowait(
                    IPCResponse(
                        res=IPCResEnum.REJECT_ABORT,
                        msg="Cannot abort: no active OTA update in progress",
                        session_id=request.session_id,
                    )
                )
                return

            if self._state == AbortState.FINAL_PHASE:
                logger.info("abort handler: rejected, update is in FINAL_PHASE")
                self._resp_queue.put_nowait(
                    IPCResponse(
                        res=IPCResEnum.REJECT_ABORT,
                        msg="Cannot abort: update is in FINAL_PHASE",
                        session_id=request.session_id,
                    )
                )
                return

            if self._state in (
                AbortState.ABORTING,
                AbortState.ABORTED,
                AbortState.REQUESTED,
            ):
                logger.info(
                    f"abort handler: accepted (already {self._state}), abort already in progress"
                )
                self._resp_queue.put_nowait(
                    IPCResponse(
                        res=IPCResEnum.ACCEPT,
                        msg="Abort already in progress",
                        session_id=request.session_id,
                    )
                )
                return

            if self._state == AbortState.CRITICAL_ZONE:
                # Queue the abort — handler returns to _run() loop and polls.
                # exit_critical_zone() will transition REQUESTED → ABORTING.
                logger.info(
                    f"abort handler: {self._state} -> {AbortState.REQUESTED} (abort queued during critical zone)"
                )
                self._state = AbortState.REQUESTED
                self._resp_queue.put_nowait(
                    IPCResponse(
                        res=IPCResEnum.ACCEPT,
                        msg="Abort queued, will proceed after critical zone exits",
                        session_id=request.session_id,
                    )
                )
                return

            # state is NONE — accept and proceed immediately
            logger.info(
                f"abort handler: {self._state} -> {AbortState.ABORTING} (immediate abort)"
            )
            self._state = AbortState.ABORTING
            self._resp_queue.put_nowait(
                IPCResponse(
                    res=IPCResEnum.ACCEPT,
                    session_id=request.session_id,
                )
            )

        # Only reached for NONE → ABORTING path
        self._perform_abort()
