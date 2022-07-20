import json
import tempfile
import time
from dataclasses import dataclass
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
        self._otaclient_enter_update = Event()
        self._otaclient_finish_update = Event()
        self._stub_cleanup_finish = Event()

    def stub_pre_update_ready(self):
        self._ota_proxy_ready.set()

    def client_wait_for_ota_proxy(self, *, timeout: Optional[float] = None):
        """Local otaclient wait for stub(to setup ota_proxy server)."""
        self._ota_proxy_ready.wait(timeout=timeout)

    def client_enter_update(self):
        self._otaclient_enter_update.set()

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

    def _pre_update(
        self, version: str, url_base: str, cookies_json: str, *, fsm: OTAUpdateFSM
    ):
        # parse cookies
        try:
            cookies: Dict[str, Any] = json.loads(cookies_json)
        except JSONDecodeError as e:
            raise InvalidUpdateRequest from e

        # set ota status
        self.updating_version = version
        self.update_phase = OTAUpdatePhase.INITIAL
        self.update_start_time = int(time.time() * 1000)  # unix time in milli-seconds
        self.failure_reason = ""  # clean failure reason

        # init ota_update_stats collector and downloader
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

        total_regular_file_size = metadata.get_total_regular_file_size()
        if total_regular_file_size:
            self.update_stats_collector.store.total_regular_file_size = (
                total_regular_file_size
            )

        # prepare update meta
        self._updatemeta = UpdateMeta(
            cookies=cookies,
            metadata=metadata,
            url_base=url_base,
            boot_dir=str(self._boot_controller.get_standby_boot_dir()),
            standby_slot_mount_point=cfg.MOUNT_POINT,
            ref_slot_mount_point=cfg.REF_ROOT_MOUNT_POINT,
        )

        # launch ota update stats collector
        self.update_stats_collector.start()

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
        """Used when ota-update is interrupted."""
        if self.update_phase is not None:
            self.update_phase = None
            self.update_stats_collector.stop()
            self._downloader.cleanup_proxy()

    def status(self) -> Optional[Tuple[str, Dict[str, Any]]]:
        """
        Returns:
            A tuple contains the version and the update_progress.
        """
        if self.update_phase is not None:
            # TODO: refactoring?
            self.update_stats_collector.store.total_elapsed_time = int(
                time.time() * 1000 - self.update_start_time
            )  # in milli-seconds

            version = self.updating_version
            update_progress = self.update_stats_collector.get_snapshot_as_dist()
            # add extra fields
            update_progress["phase"] = self.update_phase.name
            return version, update_progress

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
        except OTAError as e:
            logger.exception("update failed")
            # NOTE: also set stored ota_status to FAILURE,
            #       as the standby slot has already been cleaned up!
            self._boot_controller.store_current_ota_status(OTAStatusEnum.FAILURE)
            raise OTAUpdateError(e) from e
        finally:
            # cleanup
            self.shutdown()


@dataclass
class OTAClientStatus:
    version: str
    status: str
    update_progress: Dict[str, Any]
    failure_type: str = OTAFailureType.NO_FAILURE.name
    failure_reason: str = ""

    @staticmethod
    def _statusprogress_msg_from_dict(input: Dict[str, Any]) -> v2.StatusProgress:
        """
        expecting input dict to has the same structure as the statusprogress msg,
        unknown fields will be ignored
        """
        from numbers import Number
        from google.protobuf.duration_pb2 import Duration

        res = v2.StatusProgress()
        for k, v in input.items():
            try:
                msg_field = getattr(res, k)
            except Exception:
                continue

            if isinstance(msg_field, Number) and isinstance(v, Number):
                setattr(res, k, v)
            elif isinstance(msg_field, Duration):
                msg_field.FromMilliseconds(v)

        return res

    def export_as_v2_StatusResponseEcu(self, ecu_id: str) -> v2.StatusResponseEcu:
        ecu_status = v2.StatusResponseEcu()
        # construct top layer
        ecu_status.ecu_id = ecu_id
        ecu_status.result = v2.NO_FAILURE

        # construct status
        ecu_status.status.status = getattr(v2.StatusOta, self.status)
        ecu_status.status.failure = getattr(v2.FailureType, self.failure_type)
        ecu_status.status.failure_reason = self.failure_reason
        ecu_status.status.version = self.version

        if self.update_progress:
            prg = ecu_status.status.progress
            prg.CopyFrom(self._statusprogress_msg_from_dict(self.update_progress))
            if phase := self.update_progress.get("phase"):
                prg.phase = getattr(v2.StatusProgressPhase, phase)

        return ecu_status


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

    def _rollback(self):
        # enter rollback
        self.live_ota_status.set_ota_status(OTAStatusEnum.ROLLBACKING)
        self.failure_type = OTAFailureType.NO_FAILURE
        self.failure_reason = ""

        # leave rollback
        self.boot_controller.post_rollback()

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
        # TODO: define rollback errors
        finally:
            self._rollback_lock.release()

    def status(self) -> Optional[v2.StatusResponseEcu]:
        if self.live_ota_status.get_ota_status() == OTAStatusEnum.UPDATING:
            if _update_stats := self.updater.status():
                # construct status response dict
                _res = OTAClientStatus(
                    version=_update_stats[0],
                    status=self.live_ota_status.get_ota_status().name,
                    update_progress=_update_stats[1],
                )
                # insert failure type and failure reason
                if self.last_failure is not None:
                    _res.failure_type = self.last_failure.failure_type.name
                    _res.failure_reason = self.last_failure.get_err_reason()

                return _res.export_as_v2_StatusResponseEcu(self.my_ecu_id)
            else:
                logger.debug(
                    "live_ota_status indicates there is an ongoing update,"
                    "but we failed to get update status from updater"
                )
                return

        else:  # default status
            _res = OTAClientStatus(
                status=self.live_ota_status.get_ota_status().name,
                version=self.boot_controller.load_version(),
                update_progress={},
            )
            if self.last_failure is not None:
                _res.failure_type = self.last_failure.failure_type.name
                _res.failure_reason = self.last_failure.get_err_reason()

            return _res.export_as_v2_StatusResponseEcu(self.my_ecu_id)
