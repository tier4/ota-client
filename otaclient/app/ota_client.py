import json
import tempfile
import time
from json.decoder import JSONDecodeError
from pathlib import Path
from threading import Event, Lock
from typing import Any, Dict, Optional, Tuple, Type
from urllib.parse import urlparse

from .errors import (
    BaseOTAMetaVerificationFailed,
    NetworkError,
    OTA_APIError,
    OTAError,
    OTAProxyFailedToStart,
    OTARollbackError,
    OTAUpdateError,
)
from .boot_control import BootControllerProtocol
from .common import OTAFileCacheControl
from .configs import config as cfg
from .create_standby import StandbySlotCreatorProtocol, UpdateMeta
from .downloader import DownloadError, Downloader
from .interface import OTAClientProtocol
from .errors import InvalidUpdateRequest
from .ota_metadata import OtaMetadata
from .ota_status import LiveOTAStatus
from .proto import wrapper
from .proxy_info import proxy_cfg
from .update_stats import OTAUpdateStatsCollector
from . import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OTAUpdateFSM:
    WAIT_INTERVAL = 3

    def __init__(self) -> None:
        self._ota_proxy_ready = Event()
        self._otaclient_finish_update = Event()
        self._stub_cleanup_finish = Event()
        self.otaclient_failed = Event()
        self.otaservice_failed = Event()

    def on_otaclient_failed(self):
        self.otaclient_failed.set()

    def on_otaservice_failed(self):
        self.otaservice_failed.set()

    def stub_pre_update_ready(self):
        self._ota_proxy_ready.set()

    def client_finish_update(self):
        self._otaclient_finish_update.set()

    def stub_cleanup_finished(self):
        self._stub_cleanup_finish.set()

    def client_wait_for_ota_proxy(self) -> bool:
        """Local otaclient wait for stub(to setup ota_proxy server).

        Return:
            A bool to indicate whether the ota_proxy launching is successful or not.
        """
        while (
            not self.otaservice_failed.is_set() and not self._ota_proxy_ready.is_set()
        ):
            time.sleep(self.WAIT_INTERVAL)
        return not self.otaclient_failed.is_set()

    def client_wait_for_reboot(self) -> bool:
        """Local otaclient should reboot after the stub cleanup finished.

        Return:
            A bool indicates whether the ota_client_stub cleans up successfully.
        """
        while (
            not self.otaservice_failed.is_set()
            and not self._stub_cleanup_finish.is_set()
        ):
            time.sleep(self.WAIT_INTERVAL)
        return not self.otaclient_failed.is_set()

    def stub_wait_for_local_update(self) -> bool:
        """OTA client stub should wait for local update finish before cleanup.

        Return:
            A bool indicates whether the local update is successful or not.
        """
        while (
            not self.otaclient_failed.is_set()
            and not self._otaclient_finish_update.is_set()
        ):
            time.sleep(self.WAIT_INTERVAL)
        return not self.otaclient_failed.is_set()


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
        self.update_phase: Optional[wrapper.StatusProgressPhase] = None
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

    def _pre_update(self, url_base: str, cookies_json: str):
        # parse cookies
        try:
            cookies: Dict[str, Any] = json.loads(cookies_json)
            assert isinstance(
                cookies, dict
            ), f"invalid cookies, expecting to be parsed into dict but {type(cookies)}"
        except (JSONDecodeError, AssertionError) as e:
            raise InvalidUpdateRequest from e

        # process metadata.jwt
        logger.debug("[update] process metadata...")
        self.update_phase = wrapper.StatusProgressPhase.METADATA
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

    def _set_update_phase(self, _phase: wrapper.StatusProgressPhase):
        self.update_phase = _phase

    def _get_update_phase(self):
        return self.update_phase

    ######  public API ######

    def shutdown(self):
        self.update_phase = None
        self.update_stats_collector.stop()
        self._downloader.cleanup_proxy()

    def update_progress(self) -> Tuple[str, wrapper.StatusProgress]:
        """
        Returns:
            A tuple contains the version and the update_progress.
        """
        update_progress = self.update_stats_collector.get_snapshot()
        # set update phase
        if self.update_phase is not None:
            update_progress.phase = self.update_phase.value
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
                if not fsm.client_wait_for_ota_proxy():
                    raise OTAProxyFailedToStart("ota_proxy failed to start, abort")
                self._downloader.configure_proxy(proxy)

            # launch collector
            # init ota_update_stats collector
            self.update_stats_collector.start()

            # start the update, pre_update
            self.updating_version = version
            self.update_phase = wrapper.StatusProgressPhase.INITIAL
            self.update_start_time = time.time_ns()
            self.failure_reason = ""  # clean failure reason
            self._pre_update(url_base, cookies_json)

            # in_update
            self._in_update()
            # local update finished, set the status to POST_PROCESSING
            fsm.client_finish_update()
            self.update_phase = wrapper.StatusProgressPhase.POST_PROCESSING

            # post_update
            logger.info("[update] leaving update, wait on all subecs...")
            # NOTE: still reboot event local cleanup failed as the update itself is successful
            fsm.client_wait_for_reboot()
            self._boot_controller.post_update()
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
                self.live_ota_status.set_ota_status(wrapper.StatusOta.UPDATING)
                self.updater.execute(version, url_base, cookies_json, fsm=fsm)
            # silently ignore overlapping request
        except OTAUpdateError as e:
            self.live_ota_status.set_ota_status(wrapper.StatusOta.FAILURE)
            self.last_failure = e
            fsm.on_otaclient_failed()
        finally:
            self._update_lock.release()

    def rollback(self):
        try:
            if self._rollback_lock.acquire(blocking=False):
                logger.info("[rollback] entering...")
                self.last_failure = None
                self.live_ota_status.set_ota_status(wrapper.StatusOta.ROLLBACKING)
                self._rollback()
            # silently ignore overlapping request
        except OTARollbackError as e:
            self.live_ota_status.set_ota_status(wrapper.StatusOta.ROLLBACK_FAILURE)
            self.last_failure = e
        finally:
            self._rollback_lock.release()

    def status(self) -> wrapper.StatusResponseEcu:
        if self.live_ota_status.get_ota_status() == wrapper.StatusOta.UPDATING:
            _, _update_progress = self.updater.update_progress()
            _status = wrapper.Status(
                version=self.current_version,
                status=self.live_ota_status.get_ota_status().value,
                progress=_update_progress.unwrap(),  # type: ignore
            )
            if _last_failure := self.last_failure:
                _status.failure = _last_failure.get_err_type().value
                _status.failure_reason = _last_failure.get_err_reason()

            return wrapper.StatusResponseEcu(
                ecu_id=self.my_ecu_id,
                result=wrapper.FailureType.NO_FAILURE.value,
                status=_status.unwrap(),  # type: ignore
            )
        else:  # no update/rollback on-going
            _status = wrapper.Status(
                version=self.current_version,
                status=self.live_ota_status.get_ota_status().value,
            )
            if _last_failure := self.last_failure:
                _status.failure = _last_failure.get_err_type().value
                _status.failure_reason = _last_failure.get_err_reason()

            return wrapper.StatusResponseEcu(
                ecu_id=self.my_ecu_id,
                result=wrapper.FailureType.NO_FAILURE.value,
                status=_status.unwrap(),  # type: ignore
            )
