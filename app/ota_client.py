import json
import tempfile
import time
from json.decoder import JSONDecodeError
from pathlib import Path
from threading import Event, Lock
from typing import Any, Dict, Tuple
from urllib.parse import urlparse
from app.boot_control import BootController
from app.boot_control.common import (
    BootControlExternalError,
    BootControlInternalError,
    BootControllerProtocol,
)

from app.create_standby import (
    StandbySlotCreator,
    CreateStandbySlotExternalError,
    CreateStandbySlotInternalError,
    UpdateMeta,
)
from app.downloader import Downloader
from app.ota_status import LiveOTAStatus, OTAStatusEnum
from app.update_phase import OTAUpdatePhase
from app.interface import OTAClientInterface
from app.ota_metadata import OtaMetadata
from app.ota_error import (
    OTAOperationFailureType,
    OtaErrorBusy,
)
from app.update_stats import OTAUpdateStatsCollector
from app.configs import OTAFileCacheControl, config as cfg
from app.proxy_info import proxy_cfg
from app import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OTAUpdateFSM:
    """State machine that synchronzing ota_service and ota_client.

    States switch:
    START -> S0, caller P1_ota_service:
        ota_service start the ota_proxy,
        wait for ota_proxy to finish initializing(scrub cache),
        and then signal ota_client
    S0 -> S1, caller P2_ota_client:
        ota_client wait for ota_proxy finish intializing,
        and then finishes pre_update procedure,
        signal ota_service to send update requests to all subecus
    S1 -> S2, caller P2_ota_client:
        ota_client finishes local update,
        signal ota_service to cleanup after all subecus are ready
    S2 -> END
        ota_service finishes cleaning up,
        signal ota_client to reboot
    """

    ######## state machine definition ########
    _START, _S0, _S1, _S2 = (
        "_START",  # start
        "_S0",  # stub ready
        "_S1",  # ota_client enter update
        "_S2",  # all subECUs are ready
    )

    def __init__(self) -> None:
        self._s0 = Event()
        self._s1 = Event()
        self._s2 = Event()
        self.current: str = self._START

    def stub_ready(self):
        if self.current == self._START:
            self.current = self._S0
            self._s0.set()
        else:
            raise ValueError(f"expecting _START, but got {self.current=}")

    def client_wait_for_stub(self):
        self._s0.wait()

    def client_enter_update(self):
        self.current = self._S1
        self._s1.set()

    def stub_subecu_update_finished(self):
        self.current = self._S2
        self._s2.set()

    def client_wait_for_reboot(self):
        self._s2.wait()


class _OTAUpdator:
    def __init__(
        self,
        *,
        live_ota_status: LiveOTAStatus,
        boot_controller: BootControllerProtocol,
    ) -> None:
        self._lock = Lock()
        self._live_ota_status = live_ota_status
        self._boot_controller = boot_controller

        # pre-test whether we should enter an update
        if not self._live_ota_status.request_update():
            raise OtaErrorBusy(
                f"{self._live_ota_status.get_ota_status()} is illegal for update"
            )

        self.failure_type = OTAOperationFailureType.NO_FAILURE
        self.failure_reason = ""
        self.update_start_time: int = 0  # unix time in milli-seconds
        self.update_phase = OTAUpdatePhase.INITIAL

        self._downloader = Downloader()

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

            metadata.verify(cert_file.read_text())
            return metadata

    def _pre_update(
        self, version: str, url_base: str, cookies: Dict[str, Any], *, fsm: OTAUpdateFSM
    ):
        if self._lock.acquire(blocking=False):
            # unconditionally regulate the url_base
            _url_base = urlparse(url_base)
            _path = f"{_url_base.path.rstrip('/')}/"
            url = _url_base._replace(path=_path).geturl()

            # launch ota update statics collector
            self.update_stats = OTAUpdateStatsCollector()

            # configure proxy
            if proxy := proxy_cfg.get_proxy_for_local_ota():
                fsm.client_wait_for_stub()  # wait for stub to setup the proxy server
                self._downloader.configure_proxy(proxy)

            # process metadata.jwt
            logger.debug("[update] process metadata...")
            self.update_phase = OTAUpdatePhase.METADATA
            metadata = self._process_metadata(url, cookies)
            total_regular_file_size = metadata.get_total_regular_file_size()
            if total_regular_file_size:
                self.update_stats.store.total_regular_file_size = (
                    total_regular_file_size
                )

            # prepare update meta
            self._updatemeta = UpdateMeta(
                cookies=cookies,
                metadata=metadata,
                url_base=url,
            )

            # set ota status
            self._live_ota_status.set_ota_status(OTAStatusEnum.UPDATING)

            # set update status
            self.update_phase = OTAUpdatePhase.INITIAL
            self.failure_type = OTAOperationFailureType.NO_FAILURE
            self.update_start_time = int(time.time() * 1000)
            self.updating_version = version
            self.failure_reason = ""

            logger.info("[_pre_update] finished")
        else:
            raise OtaErrorBusy("another update request is on-going, abort")

    def _in_update(self, *, fsm: OTAUpdateFSM):
        self._live_ota_status.set_ota_status(OTAStatusEnum.UPDATING)

        # finish pre-update configuration, enter update
        fsm.client_enter_update()
        # NOTE: erase standby slot or not based on the used StandbySlotCreator
        self._boot_controller.pre_update(
            self.updating_version,
            erase_standby=StandbySlotCreator.should_erase_standby_slot(),
        )

        # configure standby slot creator
        _standby_slot_creator = StandbySlotCreator(
            update_meta=self._updatemeta,
            stats_tracker=self.update_stats,
            update_phase_tracker=self.set_update_phase,
        )
        # start to constructing standby bank
        _standby_slot_creator.create_standby_bank()
        logger.info("[_in_update] finished creating standby slot")

    def _post_update(self, *, fsm: OTAUpdateFSM):
        self.set_update_phase(OTAUpdatePhase.POST_PROCESSING)

        logger.info(
            "[update] leaving update, "
            "wait on ota_service, apply post-update and reboot..."
        )
        fsm.client_wait_for_reboot()
        self._boot_controller.post_update()

    def set_update_phase(self, _phase: OTAUpdatePhase):
        self.update_phase = _phase

    def get_update_phase(self):
        return self.update_phase

    ######  public API ######

    def status(self) -> Dict[str, Any]:
        self.update_stats.store.total_elapsed_time = (
            int(time.time() * 1000) - self.update_start_time
        )

        version = self.updating_version
        update_progress = self.update_stats.get_snapshot().export_as_dict()
        # add extra fields
        update_progress["phase"] = self.update_phase.name
        return {
            "status": self._live_ota_status.get_ota_status().name,
            "failure_type": self.failure_type.name,
            "failure_reason": self.failure_reason,
            "version": version,
            "update_progress": update_progress,
        }

    def execute(
        self,
        version: str,
        url_base: str,
        cookies: Dict[str, Any],
        *,
        fsm: OTAUpdateFSM,
    ):
        logger.info(f"{version=},{url_base=},{cookies=}")
        """
        e.g.
        cookies = {
            "CloudFront-Policy": "eyJTdGF0ZW1lbnQ...",
            "CloudFront-Signature": "o4ojzMrJwtSIg~izsy...",
            "CloudFront-Key-Pair-Id": "K2...",
        }
        """
        self._pre_update(version, url_base, cookies, fsm=fsm)
        self._in_update(fsm=fsm)
        self._post_update(fsm=fsm)


class OTAClient(OTAClientInterface):
    def __init__(self):
        self._lock = Lock()

        # boot controller
        self.boot_controller = BootController()
        self.live_ota_status = LiveOTAStatus(self.boot_controller.get_ota_status())

    def _result_ok(self):
        self.failure_type = OTAOperationFailureType.NO_FAILURE
        self.failure_reason = ""
        return OTAOperationFailureType.NO_FAILURE

    def _result_recoverable(self, e):
        logger.exception(e)
        self.failure_type = OTAOperationFailureType.RECOVERABLE
        self.failure_reason = str(e)

        self.live_ota_status.set_ota_status(OTAStatusEnum.FAILURE)
        return OTAOperationFailureType.RECOVERABLE

    def _result_unrecoverable(self, e):
        logger.exception(e)
        self.failure_type = OTAOperationFailureType.UNRECOVERABLE
        self.failure_reason = str(e)

        self.live_ota_status.set_ota_status(OTAStatusEnum.FAILURE)
        return OTAOperationFailureType.UNRECOVERABLE

    def _rollback(self):
        if self._lock.acquire(blocking=False):
            if not self.request_rollback():
                raise OtaErrorBusy(f"{self.live_ota_status=} is illegal for rollback")

            # enter rollback
            self.live_ota_status.set_ota_status(OTAStatusEnum.ROLLBACKING)
            self.failure_type = OTAOperationFailureType.NO_FAILURE
            self.failure_reason = ""

            # leave rollback
            self.boot_controller.post_rollback()
        else:
            raise OtaErrorBusy("another rollback is on-going, abort")

    ###### public API ######
    def update(
        self,
        version: str,
        url_base: str,
        cookies_json: str,
        *,
        fsm: OTAUpdateFSM,
    ):
        """
        main entry of the ota update logic
        exceptions are captured and recorded here
        """
        logger.debug("[update] entering...")

        try:
            cookies = json.loads(cookies_json)
            self.updator = _OTAUpdator(
                live_ota_status=self.live_ota_status,
                boot_controller=self.boot_controller,
            )
            self.updator.execute(version, url_base, cookies, fsm=fsm)
            return self._result_ok()
        except OtaErrorBusy:  # there is an on-going update
            # not setting ota_status
            return self._result_recoverable("update busy")
        except (
            JSONDecodeError,
            BootControlExternalError,
            CreateStandbySlotExternalError,
        ) as e:
            logger.exception(msg="recoverable")
            return self._result_recoverable(e)
        except (
            BootControlInternalError,
            CreateStandbySlotInternalError,
            Exception,
        ) as e:
            logger.exception(msg="unrecoverable")
            return self._result_unrecoverable(e)

    def rollback(self):
        try:
            self._rollback()
            return self._result_ok()
        except OtaErrorBusy:  # there is an on-going update
            # not setting ota_status
            return self._result_recoverable("rollback busy")
        except BootControlExternalError as e:
            logger.exception(msg="recoverable")
            return self._result_recoverable(e)
        except (BootControlInternalError, Exception) as e:
            logger.exception(msg="unrecoverable")
            return self._result_unrecoverable(e)

    def status(self) -> Tuple[OTAOperationFailureType, Dict[str, Any]]:
        if self.live_ota_status.get_ota_status() == OTAStatusEnum.UPDATING:
            if not hasattr(self, "updator"):
                return self._result_unrecoverable("not in update mode, abort")

            return OTAOperationFailureType.NO_FAILURE, self.updator.status()
        else:
            return OTAOperationFailureType.NO_FAILURE, {
                "status": self.live_ota_status.get_ota_status().name,
                "failure_type": self.failure_type.name,
                "failure_reason": self.failure_reason,
                "version": self.boot_controller.load_version(),
                "update_progress": {},
            }


if __name__ == "__main__":
    ota_client = OTAClient()
    ota_client.update("123.x", "http://localhost:8080", "{}")
