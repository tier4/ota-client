#!/usr/bin/env python3
from abc import ABC, abstractmethod
from pathlib import Path
import shutil

from ota_status import OtaStatus
from grub_control import GrubCtl
from constants import OtaBootStatusString, OtaStatusString
from constants import OtaBootStageAlias as StageAlias
from exceptions import OtaBootError
import configs as cfg
import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OtaBootInterface(ABC):
    """
    OtaBoot interface for implementing OtaBoot
    """

    # stage 1: check
    check_passed, check_failed = True, False
    # stage 2: finalize
    finalize_succeeded, finalize_failed = True, False

    # methods needed for boot checking and finalizing
    @abstractmethod
    def _confirm_banka(self) -> bool:
        pass

    @abstractmethod
    def _confirm_bankb(self) -> bool:
        pass

    @abstractmethod
    def _finalize_update(self) -> bool:
        pass

    @abstractmethod
    def _finalize_rollback(self) -> bool:
        pass

    @abstractmethod
    def boot(self):
        """
        main function of OtaBoot
        """
        pass

    return_value = {
        finalize_succeeded: {
            OtaStatusString.SWITCHA_STATE: (
                OtaStatusString.NORMAL_STATE,
                OtaBootStatusString.SWITCH_BOOT,
            ),
            OtaStatusString.SWITCHB_STATE: (
                OtaStatusString.NORMAL_STATE,
                OtaBootStatusString.SWITCH_BOOT,
            ),
            OtaStatusString.ROLLBACKA_STATE: (
                OtaStatusString.NORMAL_STATE,
                OtaBootStatusString.ROLLBACK_BOOT,
            ),
            OtaStatusString.ROLLBACKB_STATE: (
                OtaStatusString.NORMAL_STATE,
                OtaBootStatusString.ROLLBACK_BOOT,
            ),
        },
        finalize_failed: {
            OtaStatusString.SWITCHA_STATE: (
                OtaStatusString.UPDATE_FAIL_STATE,
                OtaBootStatusString.SWITCH_BOOT_FAIL,
            ),
            OtaStatusString.SWITCHB_STATE: (
                OtaStatusString.UPDATE_FAIL_STATE,
                OtaBootStatusString.SWITCH_BOOT_FAIL,
            ),
            OtaStatusString.ROLLBACKA_STATE: (
                OtaStatusString.ROLLBACK_FAIL_STATE,
                OtaBootStatusString.ROLLBACK_BOOT_FAIL,
            ),
            OtaStatusString.ROLLBACKB_STATE: (
                OtaStatusString.ROLLBACK_FAIL_STATE,
                OtaBootStatusString.ROLLBACK_BOOT_FAIL,
            ),
        },
        StageAlias.BYPASS_FINALIZATION_CHECK_PASSED: {},
        StageAlias.BYPASS_FINALIZATION_CHECK_FAILED: {
            OtaStatusString.SWITCHA_STATE: (
                OtaStatusString.UPDATE_FAIL_STATE,
                OtaBootStatusString.SWITCH_BOOT_FAIL,
            ),
            OtaStatusString.SWITCHB_STATE: (
                OtaStatusString.UPDATE_FAIL_STATE,
                OtaBootStatusString.SWITCH_BOOT_FAIL,
            ),
            OtaStatusString.ROLLBACKA_STATE: (
                OtaStatusString.ROLLBACK_FAIL_STATE,
                OtaBootStatusString.ROLLBACK_BOOT_FAIL,
            ),
            OtaStatusString.ROLLBACKB_STATE: (
                OtaStatusString.ROLLBACK_FAIL_STATE,
                OtaBootStatusString.ROLLBACK_BOOT_FAIL,
            ),
        },
        StageAlias.BYPASS_CHECK: {
            OtaStatusString.NORMAL_STATE: (
                OtaStatusString.NORMAL_STATE,
                OtaBootStatusString.NORMAL_BOOT,
            ),
            OtaStatusString.UPDATE_STATE: (
                OtaStatusString.UPDATE_FAIL_STATE,
                OtaBootStatusString.UPDATE_INCOMPLETE,
            ),
            OtaStatusString.PREPARED_STATE: (
                OtaStatusString.UPDATE_FAIL_STATE,
                OtaBootStatusString.UPDATE_INCOMPLETE,
            ),
            OtaStatusString.ROLLBACK_STATE: (
                OtaStatusString.ROLLBACK_FAIL_STATE,
                OtaBootStatusString.ROLLBACK_INCOMPLETE,
            ),
            OtaStatusString.UPDATE_FAIL_STATE: (
                OtaStatusString.NORMAL_STATE,
                OtaBootStatusString.NORMAL_BOOT,
            ),
        },
    }

    def __init__(self):
        """
        dynamically bind methods when the instance is being created.
        """
        # step 1: check
        self.check = {
            OtaStatusString.SWITCHA_STATE: self._confirm_banka,
            OtaStatusString.SWITCHB_STATE: self._confirm_bankb,
            OtaStatusString.ROLLBACKA_STATE: self._confirm_banka,
            OtaStatusString.ROLLBACKB_STATE: self._confirm_bankb,
        }

        # step 2: finalize
        self.finalize = {
            self.check_passed: {
                OtaStatusString.SWITCHA_STATE: self._finalize_update,
                OtaStatusString.SWITCHB_STATE: self._finalize_update,
                OtaStatusString.ROLLBACKA_STATE: self._finalize_rollback,
                OtaStatusString.ROLLBACKB_STATE: self._finalize_rollback,
            },
            self.check_failed: {},
        }


class OtaBoot(OtaBootInterface):
    """
    OTA Startup class
    """

    _grub_config_file = cfg.GRUB_CFG_FILE
    _ecuinfo_yaml_file = cfg.ECUINFO_YAML_FILE

    def __init__(self):
        """
        Initialize
        """
        super().__init__()  # methods binding
        self._ota_status = OtaStatus()
        self._grub_ctl = GrubCtl()

    def _confirm_banka(self):
        """
        confirm the current bank is A
        """
        current_bank = self._grub_ctl.get_current_bank()
        return self._grub_ctl.is_banka(current_bank)

    def _confirm_bankb(self):
        """
        confirm the current bank is B
        """
        current_bank = self._grub_ctl.get_current_bank()
        return self._grub_ctl.is_bankb(current_bank)

    def _update_finalize_ecuinfo_file(self):
        """"""
        src_file: Path = self._ecuinfo_yaml_file.with_suffix(
            self._ecuinfo_yaml_file.suffix + ".update"
        )
        dest_file: Path = self._ecuinfo_yaml_file
        if src_file.is_file():
            if dest_file.exists():
                # To do : copy for rollback
                pass
            logger.debug(f"file move: {src_file} to {dest_file}")
            shutil.move(src_file, dest_file)
        else:
            logger.error(f"file not found: {src_file}")
            return False
        return True

    def _finalize_update(self) -> bool:
        try:
            logger.debug("regenerate grub.cfg")
            logger.debug("delete custum.cfg")
            self._update_finalize_ecuinfo_file()
            self._grub_ctl.re_generate_grub_config()
            self._grub_ctl.delete_custom_cfg_file()
            self._ota_status.inc_rollback_count()
        except Exception as e:
            logger.debug(f"finalize update failed! {e}")
            raise e

        return True

    def _finalize_rollback(self) -> bool:
        try:
            logger.debug("delete custom.cfg")
            self._grub_ctl.delete_custom_cfg_file()
        except Exception as e:
            logger.debug(f"finalize rollback failed! {e}")

        return True

    def boot(self):
        status: str = self._ota_status.get_ota_status()
        logger.debug(f"Status: {status}")
        res: tuple[OtaStatusString, OtaBootStatusString]

        try:
            # step1: check
            if status in self.check:
                check_res: bool = self.check[status]()
                # step2: finalize
                if status in self.finalize[check_res]:
                    finalize_res: bool = self.finalize[check_res][status]()
                    res = self.return_value[finalize_res].get(status)
                # no finalization
                else:
                    if check_res:
                        res = self.return_value[
                            StageAlias.BYPASS_FINALIZATION_CHECK_PASSED
                        ].get(status)
                    else:
                        res = self.return_value[
                            StageAlias.BYPASS_FINALIZATION_CHECK_FAILED
                        ].get(status)
            # no check required
            else:
                res = self.return_value[StageAlias.BYPASS_CHECK].get(status)

            if res is None:
                raise OtaBootError(f"unexpected boot result.")
        except Exception as e:
            logger.error(f"otaboot failed: {e}")
            raise OtaBootError(e)

        logger.debug(f"otaboot result: {res}")
        result_state, result_boot = res
        self._ota_status.set_ota_status(result_state)
        return result_boot
