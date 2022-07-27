import json
import tempfile
import time
from json.decoder import JSONDecodeError
from pathlib import Path
from threading import Event, Lock
from typing import Any, Dict, Optional, Tuple, Type
from urllib.parse import urlparse

from app.errors import (
    BaseOTAMetaVerificationFailed,
    NetworkError,
    OTA_APIError,
    OTAError,
    OTAFailureType,
    OTARollbackError,
    OTAUpdateError,
)
from app.boot_control import BootControllerProtocol
from app.common import OTAFileCacheControl
from app.configs import config as cfg
from app.create_standby import StandbySlotCreatorProtocol, UpdateMeta
from app.downloader import DownloadError, Downloader
from app.interface import OTAClientProtocol
from app.errors import InvalidUpdateRequest
from app.ota_metadata import OtaMetadata
from app.ota_status import LiveOTAStatus, OTAStatusEnum
from app.proto import otaclient_v2_pb2 as v2
from app.proxy_info import proxy_cfg
from app.update_phase import OTAUpdatePhase
from app.update_stats import OTAUpdateStatsCollector
from app import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OTAUpdateFSM:
    def __init__(self) -> None:
        self._ota_proxy_ready = Event()
        self._otaclient_finish_update = Event()
        self._stub_cleanup_finish = Event()

    def stub_pre_update_ready(self):
        self._ota_proxy_ready.set()

    def client_wait_for_ota_proxy(self, *, timeout: Optional[float] = None):
        """Local otaclient wait for stub(to setup ota_proxy server)."""
        self._ota_proxy_ready.wait(timeout=timeout)

    def client_finish_update(self):
        self._otaclient_finish_update.set()

    def stub_wait_for_local_update(self, *, timeout: Optional[float] = None):
        self._otaclient_finish_update.wait(timeout=timeout)

    def stub_cleanup_finished(self):
        self._stub_cleanup_finish.set()

    def client_wait_for_reboot(self):
        """Local otaclient should reboot after the stub cleanup finished."""
        self._stub_cleanup_finish.wait()


class _OTAUpdater:
    """

    Attributes:
        create_standby_cls: type of create standby slot mechanism implementation
    """

    def __init__(
        self,
        boot_controller: BootControllerProtocol,
        *,
        create_standby_cls: Type[StandbySlotCreatorProtocol],
    ) -> None:
        self._boot_controller = boot_controller
        self._create_standby_cls = create_standby_cls

        # init update status
        self.update_phase: Optional[OTAUpdatePhase] = None
        self.update_start_time = 0
        self.updating_version: str = ""
        self.failure_reason = ""

        # init downloader
        self._downloader = Downloader()
        # init ota update statics collector
        self.update_stats_collector = OTAUpdateStatsCollector()

    def _process_metadata(self, url_base, cookies: Dict[str, str]):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            meta_file = Path(d) / "metadata.jwt"
            # NOTE: do not use cache when fetching metadata
            self._downloader.download(
                "metadata.jwt",
                meta_file,
                None,
                url_base=url_base,
                cookies=cookies,
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                },
            )
            metadata = OtaMetadata(meta_file.read_text())

            # download certificate and verify metadata against this certificate
            cert_info = metadata.get_certificate_info()
            cert_fname, cert_hash = cert_info["file"], cert_info["hash"]
            cert_file: Path = Path(d) / cert_fname
            self._downloader.download(
                cert_fname,
                cert_file,
                cert_hash,
                url_base=url_base,
                cookies=cookies,
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                },
            )

            metadata.verify(cert_file.read_bytes())
            return metadata

    def _pre_update(self, version: str, url_base: str, cookies_json: str):
        # parse cookies
        try:
            cookies: Dict[str, Any] = json.loads(cookies_json)
        except JSONDecodeError as e:
            raise InvalidUpdateRequest from e

        # set ota status
        self.updating_version = version
        self.update_phase = OTAUpdatePhase.INITIAL
        self.update_start_time = time.time_ns()
        self.failure_reason = ""  # clean failure reason

        # init ota_update_stats collector
        self.update_stats_collector.start(restart=True)

        # process metadata.jwt
        logger.debug("[update] process metadata...")
        self.update_phase = OTAUpdatePhase.METADATA
        try:
            metadata = self._process_metadata(url_base, cookies)
        except DownloadError as e:
            raise NetworkError("failed to download metadata") from e
        except ValueError as e:
            raise BaseOTAMetaVerificationFailed from e

        # prepare update meta
        self._updatemeta = UpdateMeta(
            cookies=cookies,
            metadata=metadata,
            url_base=url_base,
            boot_dir=str(self._boot_controller.get_standby_boot_dir()),
            standby_slot_mount_point=cfg.MOUNT_POINT,
            ref_slot_mount_point=cfg.REF_ROOT_MOUNT_POINT,
        )

        # set total_regular_file_size to stats store
        if total_regular_file_size := metadata.get_total_regular_file_size():
            self.update_stats_collector.set_total_regular_files_size(
                total_regular_file_size
            )

        # finish pre-update configuration, enter update
        # NOTE: erase standby slot or not based on the used StandbySlotCreator
        self._boot_controller.pre_update(
            self.updating_version,
            standby_as_ref=self._create_standby_cls.is_standby_as_ref(),
            erase_standby=self._create_standby_cls.should_erase_standby_slot(),
        )
        logger.info("[_pre_update] finished")

    def _in_update(self):
        # configure standby slot creator
        _standby_slot_creator = self._create_standby_cls(
            update_meta=self._updatemeta,
            stats_collector=self.update_stats_collector,
            update_phase_tracker=self._set_update_phase,
        )
        # start to constructing standby bank
        _standby_slot_creator.create_standby_slot()
        logger.info("[_in_update] finished creating standby slot")

    def _post_update(self):
        self._set_update_phase(OTAUpdatePhase.POST_PROCESSING)
        self._boot_controller.post_update()

    def _set_update_phase(self, _phase: OTAUpdatePhase):
        self.update_phase = _phase

    def _get_update_phase(self):
        return self.update_phase

    ######  public API ######

    def shutdown(self):
        self.update_phase = None
        self.update_stats_collector.stop()
        self._downloader.cleanup_proxy()

    def update_progress(self) -> Tuple[str, v2.StatusProgress]:
        """
        Returns:
            A tuple contains the version and the update_progress.
        """
        update_progress = self.update_stats_collector.get_snapshot_as_v2_StatusProgress(
            self.update_phase
        )

        # set total_elapsed_time
        update_progress.total_elapsed_time.FromNanoseconds(
            time.time_ns() - self.update_start_time
        )

        return self.updating_version, update_progress

    def execute(
        self,
        version: str,
        raw_url_base: str,
        cookies_json: str,
        *,
        fsm: OTAUpdateFSM,
    ):
        """Main entry for ota-update.


        e.g.
        cookies = {
            "CloudFront-Policy": "eyJTdGF0ZW1lbnQ...",
            "CloudFront-Signature": "o4ojzMrJwtSIg~izsy...",
            "CloudFront-Key-Pair-Id": "K2...",
        }
        """
        logger.info(f"{version=},{raw_url_base=},{cookies_json=}")

        try:
            # unconditionally regulate the url_base
            _url_base = urlparse(raw_url_base)
            _path = f"{_url_base.path.rstrip('/')}/"
            url_base = _url_base._replace(path=_path).geturl()

            # configure proxy if needed before entering update
            self._downloader.cleanup_proxy()
            if proxy := proxy_cfg.get_proxy_for_local_ota():
                logger.info("use local ota_proxy")
                # wait for stub to setup the local ota_proxy server
                fsm.client_wait_for_ota_proxy()
                self._downloader.configure_proxy(proxy)

            self._pre_update(version, url_base, cookies_json)

            self._in_update()
            fsm.client_finish_update()

            logger.info("[update] leaving update, wait on all subecs...")
            fsm.client_wait_for_reboot()

            logger.info("[update] apply post-update and reboot...")
            self._post_update()
            # NOTE: no need to call shutdown method here, keep the update_progress
            #       as it as we are still in updating before rebooting
        except OTAError as e:
            logger.error("update failed")
            self._boot_controller.on_operation_failure()
            self.shutdown()
            raise OTAUpdateError(e) from e


class OTAClient(OTAClientProtocol):
    """
    Init params:
        boot_control_cls: type of boot control mechanism to use
        create_standby_cls: type of create standby slot mechanism to use
    """

    def __init__(
        self,
        *,
        boot_control_cls: Type[BootControllerProtocol],
        create_standby_cls: Type[StandbySlotCreatorProtocol],
        my_ecu_id: str = "",
    ):
        self.my_ecu_id = my_ecu_id
        self._update_lock = Lock()  # ensure only one update session is running
        self._rollback_lock = Lock()
        self.last_failure: Optional[OTA_APIError] = None

        self.boot_controller = boot_control_cls()
        self.live_ota_status = LiveOTAStatus(self.boot_controller.get_ota_status())

        # init feature helpers
        self.updater = _OTAUpdater(
            boot_controller=self.boot_controller,
            create_standby_cls=create_standby_cls,
        )

        self.current_version = self.boot_controller.load_version()

    def _rollback(self):
        try:
            # enter rollback
            self.failure_type = OTAFailureType.NO_FAILURE
            self.failure_reason = ""
            self.boot_controller.pre_rollback()

            # leave rollback
            self.boot_controller.post_rollback()
        except OTAError as e:
            logger.error(f"rollback failed: {e!r}")
            self.boot_controller.on_operation_failure()
            raise OTARollbackError(e) from e

    ###### public API ######

    def get_last_failure(self) -> Optional[OTA_APIError]:
        return self.last_failure

    def update(
        self,
        version: str,
        url_base: str,
        cookies_json: str,
        *,
        fsm: OTAUpdateFSM,
    ):
        try:
            if self._update_lock.acquire(blocking=False):
                logger.info("[update] entering...")
                self.last_failure = None
                self.live_ota_status.set_ota_status(OTAStatusEnum.UPDATING)
                self.updater.execute(version, url_base, cookies_json, fsm=fsm)
            # silently ignore overlapping request
        except OTAUpdateError as e:
            self.live_ota_status.set_ota_status(OTAStatusEnum.FAILURE)
            self.last_failure = e
        finally:
            self._update_lock.release()

    def rollback(self):
        try:
            if self._rollback_lock.acquire(blocking=False):
                logger.info("[rollback] entering...")
                self.last_failure = None
                self.live_ota_status.set_ota_status(OTAStatusEnum.ROLLBACKING)
                self._rollback()
            # silently ignore overlapping request
        except OTARollbackError as e:
            self.live_ota_status.set_ota_status(OTAStatusEnum.ROLLBACK_FAILURE)
            self.last_failure = e
        finally:
            self._rollback_lock.release()

    def status(self) -> v2.StatusResponseEcu:
        if self.live_ota_status.get_ota_status() == OTAStatusEnum.UPDATING:
            _, _update_progress = self.updater.update_progress()
            _status = v2.Status(
                version=self.current_version,
                status=self.live_ota_status.get_ota_status().name,
                progress=_update_progress,
            )
            if self.last_failure:
                self.last_failure.register_to_v2_Status(_status)

            return v2.StatusResponseEcu(
                ecu_id=self.my_ecu_id,
                result=v2.NO_FAILURE,
                status=_status,
            )
        else:  # no update/rollback on-going
            _status = v2.Status(
                version=self.current_version,
                status=self.live_ota_status.get_ota_status().name,
            )
            if self.last_failure:
                self.last_failure.register_to_v2_Status(_status)

            return v2.StatusResponseEcu(
                ecu_id=self.my_ecu_id,
                result=v2.NO_FAILURE,
                status=_status,
            )
