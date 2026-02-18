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

import atexit
import logging
import multiprocessing.queues as mp_queue
import os
import shutil
import signal
import sys
import threading
import time
from functools import partial
from hashlib import sha256
from pathlib import Path
from queue import Empty, Queue
from typing import Callable, NoReturn, Optional

from ota_image_libs.v1.image_manifest.schema import ImageIdentifier, OTAReleaseKey

from ota_metadata.utils.cert_store import (
    CACertStoreInvalid,
    load_ca_cert_chains,
    load_ca_store,
)
from ota_metadata.utils.detect_ota_image_ver import check_if_ota_image_v1
from otaclient import errors as ota_errors
from otaclient._status_monitor import (
    OTAClientStatusCollector,
    OTAStatusChangeReport,
    SetOTAClientMetaReport,
    StatusReport,
)
from otaclient._types import (
    AbortRequestV2,
    AbortState,
    ClientUpdateControlFlags,
    ClientUpdateRequestV2,
    FailureType,
    IPCRequest,
    IPCResEnum,
    IPCResponse,
    MultipleECUStatusFlags,
    OTAStatus,
    UpdateRequestV2,
)
from otaclient._utils import (
    SharedOTAClientMetricsReader,
    SharedOTAClientStatusWriter,
    get_traceback,
)
from otaclient.boot_control import get_boot_controller
from otaclient.configs.cfg import cfg, ecu_info, proxy_info
from otaclient.metrics import OTAImageFormat, OTAMetricsData
from otaclient.ota_core._common import create_downloader_pool
from otaclient.ota_core._updater import (
    OTAUpdaterForLegacyOTAImage,
    OTAUpdaterForOTAImageV1,
)
from otaclient.ota_core._updater_base import OTAUpdateInterfaceArgs
from otaclient_common import _env
from otaclient_common.cmdhelper import ensure_mount, ensure_umount, mount_tmpfs

from ._client_updater import OTAClientUpdater
from ._common import handle_upper_proxy

logger = logging.getLogger(__name__)


OP_CHECK_INTERVAL = 1  # second
HOLD_REQ_HANDLING_ON_ACK_REQUEST = 16  # seconds
HOLD_REQ_HANDLING_ON_ACK_CLIENT_UPDATE_REQUEST = 4  # seconds
WAIT_FOR_OTAPROXY_ONLINE = 3 * 60  # 3mins
WAIT_BEFORE_DYNAMIC_CLIENT_EXIT = 6  # seconds

ABORT_SIGNAL = signal.SIGUSR1
EXIT_CODE_OTA_ABORTED = 79


def _abort_signal_handler(signum, frame):
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
        self._session_id: str | None = None

    @property
    def state(self) -> AbortState:
        with self._cond:
            return self._state

    def set_session(self, *, session_id: str) -> None:
        """Set per-session fields and reset state for the current update."""
        with self._cond:
            self._session_id = session_id
            logger.info(
                f"abort handler: {self._state.name} -> {AbortState.NONE.name} (session reset, {session_id=})"
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
                f"abort handler: {self._state.name} -> {AbortState.ABORTED.name}"
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
                f"abort handler: {self._state.name} -> {AbortState.CRITICAL_ZONE.name}"
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
                    f"abort handler: {self._state.name} -> {AbortState.ABORTING.name} (deferred abort)"
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
                    f"abort handler: {self._state.name} -> {AbortState.NONE.name} (exiting critical zone)"
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
                f"abort handler: {self._state.name} -> {AbortState.FINAL_PHASE.name}"
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
                    f"abort handler: accepted (already {self._state.name}), abort already in progress"
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
                    f"abort handler: {self._state.name} -> {AbortState.REQUESTED.name} (abort queued during critical zone)"
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
                f"abort handler: {self._state.name} -> {AbortState.ABORTING.name} (immediate abort)"
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


class OTAClient:
    """The adapter between OTAClient gRPC interface and the OTA implementation."""

    def __init__(
        self,
        *,
        ecu_status_flags: MultipleECUStatusFlags,
        proxy: Optional[str] = None,
        status_report_queue: Queue[StatusReport],
        client_update_control_flags: ClientUpdateControlFlags,
        shm_metrics_reader: SharedOTAClientMetricsReader,
    ) -> None:
        self.my_ecu_id = ecu_info.ecu_id
        self.proxy = proxy
        self.ecu_status_flags = ecu_status_flags

        self._status_report_queue = status_report_queue
        self._client_update_control_flags = client_update_control_flags

        self._shm_metrics_reader = shm_metrics_reader
        self._abort_handler: AbortHandler | None = None
        atexit.register(shm_metrics_reader.atexit)

        self._live_ota_status = OTAStatus.INITIALIZED
        self.started = False

        self._runtime_dir = _runtime_dir = Path(cfg.RUN_DIR)
        _runtime_dir.mkdir(exist_ok=True, parents=True, mode=0o700)
        self._update_session_dir = _update_session_dir = Path(cfg.RUNTIME_OTA_SESSION)

        # NOTE: for each otaclient instance lifecycle, only one tmpfs will be mounted.
        #       If otaclient terminates by signal, umounting will be handled by _on_shutdown.
        #       If otaclient exits on successful OTA, no need to umount it manually as we will reboot soon.
        ensure_umount(_update_session_dir, ignore_error=True)
        _update_session_dir.mkdir(exist_ok=True, parents=True)
        try:
            ensure_mount(
                "tmpfs",
                _update_session_dir,
                mount_func=partial(
                    mount_tmpfs, size_in_mb=cfg.SESSION_WD_TMPFS_SIZE_IN_MB
                ),
                raise_exception=True,
            )
        except Exception as e:
            logger.warning(f"failed to mount tmpfs for OTA runtime use: {e!r}")
            logger.warning("will directly use /run tmpfs for OTA runtime!")

        self._metrics = OTAMetricsData()
        self._metrics.ecu_id = self.my_ecu_id
        self._metrics.enable_local_ota_proxy_cache = (
            proxy_info.enable_local_ota_proxy_cache
        )

        try:
            _boot_controller_type = get_boot_controller(ecu_info.bootloader)
        except Exception as e:
            self._on_failure(
                e,
                ota_status=OTAStatus.FAILURE,
                failure_type=FailureType.UNRECOVERABLE,
                failure_reason=f"failed to determine boot controller or create_standby mode: {e!r}",
            )
            return

        try:
            self.boot_controller = _boot_controller_type()
        except Exception as e:
            self._on_failure(
                e,
                ota_status=OTAStatus.FAILURE,
                failure_type=FailureType.UNRECOVERABLE,
                failure_reason=f"boot controller startup failed: {e!r}",
            )
            return
        self._metrics.bootloader_type = self.boot_controller.bootloader_type

        # load and report booted OTA status
        _boot_ctrl_loaded_ota_status = self.boot_controller.get_booted_ota_status()
        self._live_ota_status = _boot_ctrl_loaded_ota_status
        self.current_version = self.boot_controller.load_version()

        status_report_queue.put_nowait(
            StatusReport(
                payload=SetOTAClientMetaReport(
                    firmware_version=self.current_version,
                ),
            )
        )
        status_report_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=_boot_ctrl_loaded_ota_status,
                ),
            )
        )
        self._metrics.current_firmware_version = self.current_version

        self.ca_chains_store = None
        try:
            self.ca_chains_store = load_ca_cert_chains(cfg.CERT_DPATH)
        except CACertStoreInvalid as e:
            _err_msg = (
                f"failed to import ca_chains_store: {e!r}, "
                "OTA with legacy OTA image will NOT occur on no CA chains installed!!!"
            )
            logger.error(_err_msg)

        self.ca_store = None
        try:
            self.ca_store = load_ca_store(cfg.CERT_DPATH)
        except CACertStoreInvalid as e:
            _err_msg = (
                f"failed to import ca_store: {e!r}, "
                "OTA with OTA image v1 will NOT occur on no CA store installed!!!"
            )
            logger.error(_err_msg)

        self.started = True
        logger.info("otaclient started")

    def _on_failure(
        self,
        exc: Exception,
        *,
        ota_status: OTAStatus,
        failure_reason: str,
        failure_type: FailureType,
    ) -> None:
        try:
            _traceback = get_traceback(exc)

            logger.error(failure_reason)
            logger.error(f"last error traceback: \n{_traceback}")

            self._status_report_queue.put_nowait(
                StatusReport(
                    payload=OTAStatusChangeReport(
                        new_ota_status=ota_status,
                        failure_type=failure_type,
                        failure_reason=failure_reason,
                        failure_traceback=_traceback,
                    ),
                )
            )
            self._metrics.failure_type = failure_type
            self._metrics.failure_reason = failure_reason
            self._metrics.failed_status = ota_status
        finally:
            del exc  # prevent ref cycle

    def _exit_from_dynamic_client(self) -> None:
        """Exit from dynamic client."""
        if not _env.is_dynamic_client_running():
            # dynamic client is not running, no need to exit
            return

        time.sleep(WAIT_BEFORE_DYNAMIC_CLIENT_EXIT)
        logger.info("exit from dynamic client...")
        self._client_update_control_flags.request_shutdown_event.set()

    # API

    @property
    def live_ota_status(self) -> OTAStatus:
        return self._live_ota_status

    @property
    def is_busy(self) -> bool:
        return self._live_ota_status in [
            OTAStatus.UPDATING,
            OTAStatus.ROLLBACKING,
            OTAStatus.CLIENT_UPDATING,
            OTAStatus.ABORTING,
        ]

    def report_status(self, report: StatusReport) -> None:
        """Submit a status report to the status report queue."""
        self._status_report_queue.put_nowait(report)

    def update(self, request: UpdateRequestV2) -> None:
        """
        NOTE that update API will not raise any exceptions. The failure information
            is available via status API.
        """
        request_id = request.request_id
        new_session_id = request.session_id
        logger.info(
            f"start new OTA update request:{request_id}, session: {new_session_id=}"
        )

        session_wd = self._update_session_dir / new_session_id

        # Configure abort handler before setting UPDATING status so that
        # session fields are populated before the abort handler would
        # accept any requests (it checks live_ota_status == UPDATING).
        self._abort_handler.set_session(session_id=new_session_id)

        # NOTE(20250916): set OTA update status before ensuring upper otaproxy
        #                 as local otaproxy needs OTA update status to start.
        self._live_ota_status = OTAStatus.UPDATING
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=OTAStatus.UPDATING,
                ),
                session_id=new_session_id,
            )
        )

        if self.proxy:
            handle_upper_proxy(self.proxy)

        self._metrics.request_id = request_id
        self._metrics.session_id = new_session_id

        download_pool = create_downloader_pool(
            request.cookies_json,
            self.proxy,
            download_threads=cfg.DOWNLOAD_THREADS,
            hash_func=sha256,
            chunk_size=cfg.CHUNK_SIZE,
        )
        url_base = request.url_base

        try:
            logger.info("[update] entering local update...")
            _common_args = OTAUpdateInterfaceArgs(
                version=request.version,
                raw_url_base=request.url_base,
                session_wd=session_wd,
                downloader_pool=download_pool,
                ecu_status_flags=self.ecu_status_flags,
                status_report_queue=self._status_report_queue,
                session_id=new_session_id,
                metrics=self._metrics,
                shm_metrics_reader=self._shm_metrics_reader,
            )

            _no_ca_err = "no CA chains are installed, reject any OTA update"
            if check_if_ota_image_v1(url_base, downloader_pool=download_pool):
                logger.info(f"{url_base} hosts new OTA image version1")
                self._metrics.ota_image_format = OTAImageFormat.V1
                if not self.ca_store:
                    raise ota_errors.MetadataJWTVerficationFailed(
                        _no_ca_err, module=__name__
                    )

                # NOTE(20251009): currently the update API still doesn't support specify
                #                 the image varient, provide a default value here.
                image_id = ImageIdentifier(
                    ecu_id=ecu_info.ecu_id,
                    release_key=OTAReleaseKey.dev,
                )
                logger.info(f"selecting image payload {image_id} from OTA image")

                OTAUpdaterForOTAImageV1(
                    ca_store=self.ca_store,
                    abort_handler=self._abort_handler,
                    boot_controller=self.boot_controller,
                    image_identifier=image_id,
                    **_common_args,
                ).execute()
            else:
                logger.info(f"{url_base} hosts legacy OTA image")
                self._metrics.ota_image_format = OTAImageFormat.LEGACY
                if not self.ca_chains_store:
                    raise ota_errors.MetadataJWTVerficationFailed(
                        _no_ca_err, module=__name__
                    )
                OTAUpdaterForLegacyOTAImage(
                    ca_chains_store=self.ca_chains_store,
                    abort_handler=self._abort_handler,
                    boot_controller=self.boot_controller,
                    **_common_args,
                ).execute()
        except ota_errors.OTAAbortSignal:
            self._live_ota_status = OTAStatus.ABORTED
            self._status_report_queue.put_nowait(
                StatusReport(
                    payload=OTAStatusChangeReport(
                        new_ota_status=OTAStatus.ABORTED,
                    ),
                    session_id=new_session_id,
                )
            )
            logger.info("OTA update aborted by abort handler")
        except ota_errors.OTAError as e:
            self._live_ota_status = OTAStatus.FAILURE
            self._on_failure(
                e,
                ota_status=OTAStatus.FAILURE,
                failure_reason=e.get_failure_reason(),
                failure_type=e.failure_type,
            )
        finally:
            if self._abort_handler.state not in (
                AbortState.ABORTING,
                AbortState.ABORTED,
            ):
                shutil.rmtree(session_wd, ignore_errors=True)
            try:
                if self._shm_metrics_reader:
                    _shm_metrics = self._shm_metrics_reader.sync_msg()
                    self._metrics.shm_merge(_shm_metrics)
            except Exception as e:
                logger.error(f"failed to merge metrics: {e!r}")
            self._metrics.publish()

            self._exit_from_dynamic_client()

    def client_update(self, request: ClientUpdateRequestV2) -> None:
        """
        NOTE that client update API will not raise any exceptions. The failure information
            is available via status API.
        """
        if _env.is_running_as_downloaded_dynamic_app():
            # Duplicates client update should not be allowed.
            # TODO(airkei) [2025-06-19]: should return the dedicated error code for "client update"
            logger.warning(
                "duplicated dynamic otaclient update is not allowed, ignored"
            )
            return

        request_id = request.request_id
        new_session_id = request.session_id
        logger.info(
            f"start new OTA client update request: {request_id}, session: {new_session_id=}"
        )

        # NOTE(20250916): set OTA update status before ensuring upper otaproxy
        #                 as local otaproxy needs OTA update status to start.
        self._live_ota_status = OTAStatus.CLIENT_UPDATING
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=OTAStatus.CLIENT_UPDATING,
                ),
                session_id=new_session_id,
            )
        )

        if self.proxy:
            handle_upper_proxy(self.proxy)

        download_pool = create_downloader_pool(
            request.cookies_json,
            self.proxy,
            download_threads=cfg.DOWNLOAD_THREADS,
            hash_func=sha256,
            chunk_size=cfg.CHUNK_SIZE,
        )
        session_wd = self._update_session_dir / new_session_id
        try:
            logger.info("[client update] entering local update...")
            if not self.ca_chains_store:
                raise ota_errors.MetadataJWTVerficationFailed(
                    "no CA chains are installed, reject any OTA update",
                    module=__name__,
                )

            OTAClientUpdater(
                version=request.version,
                raw_url_base=request.url_base,
                session_wd=session_wd,
                standby_slot_dev=self.boot_controller.standby_slot_dev,
                ca_chains_store=self.ca_chains_store,
                ecu_status_flags=self.ecu_status_flags,
                status_report_queue=self._status_report_queue,
                downloader_pool=download_pool,
                session_id=new_session_id,
                client_update_control_flags=self._client_update_control_flags,
                metrics=self._metrics,
                shm_metrics_reader=self._shm_metrics_reader,
            ).execute()
        except ota_errors.OTAError:
            logger.warning("client update failed")
            # TODO(airkei) [2025-06-19]: should return the dedicated error code for "client update"
            # As temporary workaround, we set the status to SUCCESS here when current process is dynamic client.
            self._live_ota_status = OTAStatus.SUCCESS
            self._status_report_queue.put_nowait(
                StatusReport(
                    payload=OTAStatusChangeReport(
                        new_ota_status=OTAStatus.SUCCESS,
                    ),
                    session_id=new_session_id,
                )
            )
        except Exception as e:
            logger.error(
                f"Exception occurred while doing client update! Begin Shutdown process. Error: {e!r}"
            )
            self._client_update_control_flags.request_shutdown_event.set()
        finally:
            shutil.rmtree(session_wd, ignore_errors=True)

    def main(
        self,
        *,
        req_queue: mp_queue.Queue[IPCRequest],
        resp_queue: mp_queue.Queue[IPCResponse],
    ) -> NoReturn:
        """Main loop of ota_core process."""
        self._abort_handler = AbortHandler(
            ota_client=self,
        )
        self._abort_handler.start()

        _allow_request_after = 0
        while True:
            _now = int(time.time())
            try:
                request = req_queue.get(timeout=OP_CHECK_INTERVAL)
            except Empty:
                continue

            if isinstance(request, AbortRequestV2):
                self._abort_handler.submit(request)
                _resp = self._abort_handler.get_response()
                if _resp is None:
                    _resp = IPCResponse(
                        res=IPCResEnum.REJECT_ABORT,
                        msg="abort handler did not respond in time",
                        session_id=request.session_id,
                    )
                resp_queue.put_nowait(_resp)
                continue

            if _now < _allow_request_after or self.is_busy:
                _err_msg = (
                    f"otaclient is busy at {self._live_ota_status} or "
                    f"request too quickly({_allow_request_after=}), "
                    f"reject {request}"
                )
                logger.warning(_err_msg)
                resp_queue.put_nowait(
                    IPCResponse(
                        res=IPCResEnum.REJECT_BUSY,
                        msg=_err_msg,
                        session_id=request.session_id,
                    )
                )

            elif isinstance(request, UpdateRequestV2):
                _update_thread = threading.Thread(
                    target=self.update,
                    args=[request],
                    daemon=True,
                    name="ota_update_executor",
                )
                _update_thread.start()

                resp_queue.put_nowait(
                    IPCResponse(
                        res=IPCResEnum.ACCEPT,
                        session_id=request.session_id,
                    )
                )
                _allow_request_after = _now + HOLD_REQ_HANDLING_ON_ACK_REQUEST

            elif isinstance(request, ClientUpdateRequestV2):
                _client_update_thread = threading.Thread(
                    target=self.client_update,
                    args=[request],
                    daemon=True,
                    name="ota_client_update_executor",
                )
                _client_update_thread.start()

                resp_queue.put_nowait(
                    IPCResponse(
                        res=IPCResEnum.ACCEPT,
                        session_id=request.session_id,
                    )
                )
                _allow_request_after = (
                    _now + HOLD_REQ_HANDLING_ON_ACK_CLIENT_UPDATE_REQUEST
                )

            else:
                _err_msg = f"request is invalid: {request=}, {self._live_ota_status=}"
                logger.error(_err_msg)
                resp_queue.put_nowait(
                    IPCResponse(
                        res=IPCResEnum.REJECT_OTHER,
                        msg=_err_msg,
                        session_id=request.session_id,
                    )
                )


def ota_core_process(
    *,
    shm_writer_factory: Callable[[], SharedOTAClientStatusWriter],
    shm_metrics_reader_factory: Callable[[], SharedOTAClientMetricsReader],
    ecu_status_flags: MultipleECUStatusFlags,
    op_queue: mp_queue.Queue[IPCRequest],
    resp_queue: mp_queue.Queue[IPCResponse],
    max_traceback_size: int,  # in bytes
    client_update_control_flags: ClientUpdateControlFlags,
):
    from otaclient._logging import configure_logging
    from otaclient.configs.cfg import proxy_info

    configure_logging()

    # Register signal handler for abort-triggered process termination.
    # SIGUSR1 handler calls sys.exit(EXIT_CODE_OTA_ABORTED) which propagates
    # cleanly as SystemExit in the main thread; daemon threads die automatically.
    signal.signal(ABORT_SIGNAL, _abort_signal_handler)

    shm_writer = shm_writer_factory()
    shm_metrics_reader = shm_metrics_reader_factory()

    _local_status_report_queue = Queue()
    _status_monitor = OTAClientStatusCollector(
        msg_queue=_local_status_report_queue,
        shm_status=shm_writer,
        max_traceback_size=max_traceback_size,
    )
    _status_monitor.start()
    _status_monitor.start_log_thread()

    _ota_core = OTAClient(
        ecu_status_flags=ecu_status_flags,
        proxy=proxy_info.get_proxy_for_local_ota(),
        status_report_queue=_local_status_report_queue,
        client_update_control_flags=client_update_control_flags,
        shm_metrics_reader=shm_metrics_reader,
    )
    _ota_core.main(
        req_queue=op_queue,
        resp_queue=resp_queue,
    )
