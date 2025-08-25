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
import shutil
import threading
import time
from functools import partial
from pathlib import Path
from queue import Empty, Queue
from typing import Callable, NoReturn, Optional

from ota_metadata.utils.cert_store import (
    CACertStoreInvalid,
    CAChainStore,
    load_ca_cert_chains,
)
from otaclient import errors as ota_errors
from otaclient._status_monitor import (
    OTAClientStatusCollector,
    OTAStatusChangeReport,
    SetOTAClientMetaReport,
    StatusReport,
)
from otaclient._types import (
    ClientUpdateControlFlags,
    ClientUpdateRequestV2,
    CriticalZoneFlags,
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
from otaclient.configs._cfg_consts import CANONICAL_ROOT
from otaclient.configs.cfg import cfg, ecu_info, proxy_info
from otaclient.metrics import OTAMetricsData
from otaclient_common import _env
from otaclient_common.cmdhelper import ensure_mount, ensure_umount, mount_tmpfs
from otaclient_common.linux import fstrim_at_subprocess

from ._client_updater import OTAClientUpdater
from ._updater import OTAUpdater

logger = logging.getLogger(__name__)


OP_CHECK_INTERVAL = 1  # second
HOLD_REQ_HANDLING_ON_ACK_REQUEST = 16  # seconds
HOLD_REQ_HANDLING_ON_ACK_CLIENT_UPDATE_REQUEST = 4  # seconds
WAIT_FOR_OTAPROXY_ONLINE = 3 * 60  # 3mins


class OTAClient:
    """The adapter between OTAClieng gRPC interface and the OTA implementation."""

    def __init__(
        self,
        *,
        ecu_status_flags: MultipleECUStatusFlags,
        proxy: Optional[str] = None,
        status_report_queue: Queue[StatusReport],
        client_update_control_flags: ClientUpdateControlFlags,
        critical_zone_flags: CriticalZoneFlags,
        shm_metrics_reader: SharedOTAClientMetricsReader,
    ) -> None:
        self.my_ecu_id = ecu_info.ecu_id
        self.proxy = proxy
        self.ecu_status_flags = ecu_status_flags

        self._status_report_queue = status_report_queue
        self._client_update_control_flags = client_update_control_flags
        self._critical_zone_flags = critical_zone_flags

        self._shm_metrics_reader = shm_metrics_reader
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
            _err_msg = f"failed to import ca_chains_store: {e!r}, OTA will NOT occur on no CA chains installed!!!"
            logger.error(_err_msg)

            self.ca_chains_store = CAChainStore()

        self.started = True
        logger.info("otaclient started")

        # NOTE: not doing fstrim at startup when running as dynamic otaclient
        if not _env.is_dynamic_client_running() and cfg.FSTRIM_AT_OTACLIENT_STARTUP:
            logger.info(
                "spawn a subprocess to do fstrim on active slot"
                f"(timeout={cfg.FSTRIM_AT_OTACLIENT_STARTUP_TIMEOUT}s)"
            )
            fstrim_at_subprocess(
                Path(CANONICAL_ROOT),
                wait=False,
                timeout=cfg.FSTRIM_AT_OTACLIENT_STARTUP_TIMEOUT,
            )

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
        ]

    def update(self, request: UpdateRequestV2) -> None:
        """
        NOTE that update API will not raise any exceptions. The failure information
            is available via status API.
        """
        self._live_ota_status = OTAStatus.UPDATING
        request_id = request.request_id
        new_session_id = request.session_id
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=OTAStatus.UPDATING,
                ),
                session_id=new_session_id,
            )
        )
        logger.info(
            f"start new OTA update request:{request_id}, session: {new_session_id=}"
        )

        session_wd = self._update_session_dir / new_session_id
        self._metrics.request_id = request_id
        self._metrics.session_id = new_session_id
        try:
            logger.info("[update] entering local update...")
            if not self.ca_chains_store:
                raise ota_errors.MetadataJWTVerficationFailed(
                    "no CA chains are installed, reject any OTA update",
                    module=__name__,
                )

            OTAUpdater(
                version=request.version,
                raw_url_base=request.url_base,
                cookies_json=request.cookies_json,
                session_wd=session_wd,
                ca_chains_store=self.ca_chains_store,
                boot_controller=self.boot_controller,
                ecu_status_flags=self.ecu_status_flags,
                critical_zone_flags=self._critical_zone_flags,
                upper_otaproxy=self.proxy,
                status_report_queue=self._status_report_queue,
                session_id=new_session_id,
                metrics=self._metrics,
                shm_metrics_reader=self._shm_metrics_reader,
            ).execute()
        except ota_errors.OTAError as e:
            self._live_ota_status = OTAStatus.FAILURE
            self._on_failure(
                e,
                ota_status=OTAStatus.FAILURE,
                failure_reason=e.get_failure_reason(),
                failure_type=e.failure_type,
            )
            self._exit_from_dynamic_client()
        finally:
            shutil.rmtree(session_wd, ignore_errors=True)
            try:
                if self._shm_metrics_reader:
                    _shm_metrics = self._shm_metrics_reader.sync_msg()
                    self._metrics.shm_merge(_shm_metrics)
            except Exception as e:
                logger.error(f"failed to merge metrics: {e!r}")
            self._metrics.publish()

    def client_update(self, request: ClientUpdateRequestV2) -> None:
        """
        NOTE that client update API will not raise any exceptions. The failure information
            is available via status API.
        """
        if _env.is_dynamic_client_running():
            # Duplicates client update should not be allowed.
            # TODO(airkei) [2025-06-19]: should return the dedicated error code for "client update"
            return

        self._live_ota_status = OTAStatus.CLIENT_UPDATING
        request_id = request.request_id
        new_session_id = request.session_id
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=OTAStatus.CLIENT_UPDATING,
                ),
                session_id=new_session_id,
            )
        )
        logger.info(
            f"start new OTA client update request: {request_id}, session: {new_session_id=}"
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
                cookies_json=request.cookies_json,
                session_wd=session_wd,
                ca_chains_store=self.ca_chains_store,
                ecu_status_flags=self.ecu_status_flags,
                upper_otaproxy=self.proxy,
                status_report_queue=self._status_report_queue,
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
        except Exception:
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
        _allow_request_after = 0
        while True:
            _now = int(time.time())
            try:
                request = req_queue.get(timeout=OP_CHECK_INTERVAL)
            except Empty:
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
    critical_zone_flags: CriticalZoneFlags,
):
    from otaclient._logging import configure_logging
    from otaclient.configs.cfg import proxy_info

    configure_logging()

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
        critical_zone_flags=critical_zone_flags,
        shm_metrics_reader=shm_metrics_reader,
    )
    _ota_core.main(req_queue=op_queue, resp_queue=resp_queue)
