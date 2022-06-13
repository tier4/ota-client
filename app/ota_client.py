import json
import tempfile
import time
from contextlib import contextmanager
from json.decoder import JSONDecodeError
from pathlib import Path
from threading import Event, Lock
from typing import Any, Dict, Tuple
from urllib.parse import urlparse
from app.boot_control import BootController

from app.create_standby import StandbySlotCreator
from app.create_standby.common import UpdateMeta
from app.downloader import Downloader
from app.ota_status import LiveOTAStatusMixin, OTAStatusEnum
from app.update_phase import OTAUpdatePhase
from app.interface import OTAClientInterface
from app.ota_metadata import OtaMetadata
from app.ota_error import (
    OTAOperationFailureType,
    OtaErrorUnrecoverable,
    OtaErrorRecoverable,
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
    _START, _S0, _S1, _S2, _END = (
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
        if self.current == self._S1:
            self.current = self._S2
            self._s2.set()
        else:
            raise ValueError(f"expecting _S1, but got {self.current=}")

    def client_wait_for_reboot(self):
        self._s2.wait()


class OTAClient(LiveOTAStatusMixin, OTAClientInterface):
    def __init__(self):
        self._lock = Lock()

        # TODO: make the following as a class
        self.failure_type = OTAOperationFailureType.NO_FAILURE
        self.failure_reason = ""
        self.update_start_time: int = 0  # unix time in milli-seconds
        self.update_phase = OTAUpdatePhase.INITIAL

        self._downloader = Downloader()

        # boot controller
        self.boot_controller = BootController()
        self.live_ota_status = self.boot_controller.get_ota_status()

    def _result_ok(self):
        self.failure_type = OTAOperationFailureType.NO_FAILURE
        self.failure_reason = ""
        return OTAOperationFailureType.NO_FAILURE

    def _result_recoverable(self, e):
        logger.exception(e)
        self.failure_type = OTAOperationFailureType.RECOVERABLE
        self.failure_reason = str(e)

        self.set_live_ota_status(OTAStatusEnum.FAILURE)
        return OTAOperationFailureType.RECOVERABLE

    def _result_unrecoverable(self, e):
        logger.exception(e)
        self.failure_type = OTAOperationFailureType.UNRECOVERABLE
        self.failure_reason = str(e)

        self.set_live_ota_status(OTAStatusEnum.FAILURE)
        return OTAOperationFailureType.UNRECOVERABLE

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

    def _update(
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
        if self._lock.acquire(blocking=False):
            # check whether we can start an OTA update or not,
            # if not, the request_update method will raise OTAUpdateBusyError
            if not self.request_update():
                raise OtaErrorBusy(f"{self.live_ota_status=} is illegal for update")

            # launch ota update statics collector
            self._ota_statics_collector = OTAUpdateStatsCollector()

            # configure proxy
            if proxy := proxy_cfg.get_proxy_for_local_ota():
                fsm.client_wait_for_stub()  # wait for stub to setup the proxy server
                self._downloader.configure_proxy(proxy)

            # process metadata.jwt
            logger.debug("[update] process metadata...")
            self._update_phase = OTAUpdatePhase.METADATA
            metadata = self._process_metadata(url, cookies)
            total_regular_file_size = metadata.get_total_regular_file_size()
            if total_regular_file_size:
                self._ota_statics_collector.set(
                    "total_regular_file_size", total_regular_file_size
                )

            # unconditionally regulate the url_base
            _url_base = urlparse(url_base)
            _path = f"{_url_base.path.rstrip('/')}/"
            url = _url_base._replace(path=_path).geturl()

            # set ota status
            self.set_live_ota_status(OTAStatusEnum.UPDATING)

            # set update status
            # TODO: make the following stats as a class?
            self.update_phase = OTAUpdatePhase.INITIAL
            self.failure_type = OTAOperationFailureType.NO_FAILURE
            self.update_start_time = int(time.time() * 1000)
            self.updating_version = version
            self.failure_reason = ""
        else:
            raise OtaErrorBusy("another update request is on-going, abort")

        # finish pre-update configuration, enter update
        fsm.client_enter_update()
        # NOTE: erase standby slot or not based on the used StandbySlotCreator
        self.boot_controller.pre_update(
            version, erase_standby=StandbySlotCreator.should_erase_standby_slot()
        )
        self.set_live_ota_status(OTAStatusEnum.UPDATING)

        # configure standby slot creator
        _update_meta = UpdateMeta(
            cookies=cookies,
            metadata=metadata,
            url_base=url,
            standby_slot=self.boot_controller.get_standby_slot_path(),
            reference_slot=None,  # TODO: get reference slot
            boot_dir=None,  # TODO: get boot dir
        )

        _standby_slot_creator = StandbySlotCreator(
            update_meta=_update_meta,
            stats_tracker=self._ota_statics_collector,
            status_updator=self.set_update_phase,
        )
        # start to constructing standby bank
        _standby_slot_creator.create_standby_bank()

        # standby slot preparation finished, set phase to POST_PROCESSING
        logger.info("[update] local update finished, entering post-update...")
        self.set_update_phase(OTAUpdatePhase.POST_PROCESSING)

        logger.info(
            "[update] leaving update, "
            "wait on ota_service, apply post-update and reboot..."
        )
        fsm.client_wait_for_reboot()
        self.boot_controller.post_update()

    def _rollback(self):
        if self._lock.acquire(blocking=False):
            if not self.request_rollback():
                raise OtaErrorBusy(f"{self.live_ota_status=} is illegal for rollback")

            # enter rollback
            self.set_live_ota_status(OTAStatusEnum.ROLLBACKING)
            self.failure_type = OTAOperationFailureType.NO_FAILURE
            self.failure_reason = ""

            # leave rollback
            self.boot_controller.post_rollback()
        else:
            raise OtaErrorBusy(f"another rollback is on-going, abort")

    def _status(self) -> Dict[str, Any]:
        # TODO: refactoring
        if self.get_live_ota_status() == OTAStatusEnum.UPDATING:
            total_elapsed_time = int(time.time() * 1000) - self.update_start_time
            self._ota_statics_collector.set("total_elapsed_time", total_elapsed_time)
            version = self.updating_version

            update_progress = (
                self._ota_statics_collector.get_snapshot().export_as_dict()
            )
            # add extra fields
            update_progress["phase"] = self._update_phase.name

            return {
                "status": self.get_live_ota_status().name,
                "failure_type": self.failure_type.name,
                "failure_reason": self.failure_reason,
                "version": version,
                "update_progress": update_progress,
            }
        else:
            return {
                "status": self.get_live_ota_status().name,
                "failure_type": self.failure_type,
                "failure_reason": self.failure_reason,
                "version": self.boot_controller.load_version(),
                "update_progress": {},
            }

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
            self._update(version, url_base, cookies, fsm=fsm)
            return self._result_ok()
        except OtaErrorBusy:  # there is an on-going update
            # not setting ota_status
            logger.exception("update busy")
            return self._result_recoverable("update busy")
        except (JSONDecodeError, OtaErrorRecoverable) as e:
            logger.exception(msg="recoverable")
            return self._result_recoverable(e)
        except (OtaErrorUnrecoverable, Exception) as e:
            logger.exception(msg="unrecoverable")
            return self._result_unrecoverable(e)

    def rollback(self):
        try:
            self._rollback()
            return self._result_ok()
        except OtaErrorBusy:  # there is an on-going update
            # not setting ota_status
            logger.exception("rollback busy")
            return self._result_recoverable("rollback busy")
        except OtaErrorRecoverable as e:
            logger.exception(msg="recoverable")
            return self._result_recoverable(e)
        except (OtaErrorUnrecoverable, Exception) as e:
            logger.exception(msg="unrecoverable")
            return self._result_unrecoverable(e)

    # NOTE: status should not update any internal status
    def status(self):
        try:
            status = self._status()
            return OTAOperationFailureType.NO_FAILURE, status
        except OtaErrorRecoverable:
            logger.exception("recoverable")
            return OTAOperationFailureType.RECOVERABLE, None
        except (OtaErrorUnrecoverable, Exception):
            logger.exception("unrecoverable")
            return OTAOperationFailureType.UNRECOVERABLE, None

    def set_update_phase(self, _phase: OTAUpdatePhase):
        self.update_phase = _phase

    def get_update_phase(self):
        return self.update_phase


if __name__ == "__main__":
    ota_client = OTAClient()
    ota_client.update("123.x", "http://localhost:8080", "{}")
