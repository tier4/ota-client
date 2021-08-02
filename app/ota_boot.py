#!/usr/bin/env python3

import os
import shutil

import ota_status
import grub_control

from logging import getLogger, INFO, DEBUG

logger = getLogger(__name__)
logger.setLevel(INFO)


class OtaBoot:
    """
    OTA Startup class
    """

    # boot status
    NORMAL_BOOT = "NORMAL_BOOT"
    SWITCH_BOOT = "SWITCH_BOOT"
    SWITCH_BOOT_FAIL = "SWITCH_BOOT_FAIL"
    ROLLBACK_BOOT = "ROLLBACK_BOOT"
    ROLLBACK_BOOT_FAIL = "ROLLBACK_BOOT_FAIL"
    UPDATE_INCOMPLETE = "UPDATE_INCOMPLETE"
    ROLLBACK_INCOMPLETE = "ROLLBACK_INCOMPLETE"

    def __init__(
        self,
        ota_status_file="/boot/ota/ota_status",
        default_grub_file="/etc/default/grub",
        grub_config_file="/boot/grub/grub.cfg",
        custom_config_file="/boot/grub/custom.cfg",
        bank_info_file="/boot/ota/bankinfo.yaml",
        fstab_file="/etc/fstab",
        ecuinfo_yaml_file="/boot/ota/ecuinfo.yaml",
        ota_rollback_file="/boot/ota/ota_rollback_count",
    ):
        """
        Initialize
        """
        self._grub_cfg_file = grub_config_file
        self.__ecuinfo_yaml_file = ecuinfo_yaml_file
        self._ota_status = ota_status.OtaStatus(
            ota_status_file=ota_status_file, ota_rollback_file=ota_rollback_file
        )
        self._grub_ctl = grub_control.GrubCtl(
            default_grub_file=default_grub_file,
            grub_config_file=grub_config_file,
            custom_config_file=custom_config_file,
            bank_info_file=bank_info_file,
            fstab_file=fstab_file,
        )

    def _confirm_banka(self):
        """
        confirm the current bank is A
        """
        current_bank = self._grub_ctl._bank_info.get_current_bank()
        return self._grub_ctl._bank_info.is_banka(current_bank)

    def _confirm_bankb(self):
        """
        confirm the current bank is B
        """
        current_bank = self._grub_ctl._bank_info.get_current_bank()
        return self._grub_ctl._bank_info.is_bankb(current_bank)

    def _update_finalize_ecuinfo_file(self):
        """"""
        src_file = self.__ecuinfo_yaml_file + ".update"
        dest_file = self.__ecuinfo_yaml_file
        if os.path.isfile(src_file):
            if os.path.exists(dest_file):
                # To do : copy for rollback
                pass
            logger.debug(f"file move: {src_file} to {dest_file}")
            shutil.move(src_file, dest_file)
        else:
            logger.error(f"file not found: {src_file}")
            return False
        return True

    def boot(self):
        status = self._ota_status.get_ota_status()
        logger.debug(f"Status: {status}")
        result_boot, result_state = self._boot(status)
        self._ota_status.set_ota_status(result_state)
        return result_boot

    def _finalize_update(self):
        logger.debug("regenerate grub.cfg")
        logger.debug("delete custum.cfg")
        self._update_finalize_ecuinfo_file()
        self._grub_ctl.re_generate_grub_config()
        self._grub_ctl.delete_custom_cfg_file()
        self._ota_status.inc_rollback_count()

    def _finalize_rollback(self):
        logger.debug("delete custom.cfg")
        self._grub_ctl.delete_custom_cfg_file()

    def _boot(self, status):
        """
        OTA boot
        """
        from ota_status import OtaStatus

        status_checker_key = "status_checker"
        success_key = "success"
        failure_key = "failure"
        state_key = "state"
        boot_key = "boot"
        finalize_key = "finalize"

        state_table = {
            OtaStatus.NORMAL_STATE: {
                success_key: {
                    state_key: OtaStatus.NORMAL_STATE,
                    boot_key: OtaBoot.NORMAL_BOOT,
                },
            },
            OtaStatus.SWITCHA_STATE: {
                status_checker_key: self._confirm_banka,
                success_key: {
                    state_key: OtaStatus.NORMAL_STATE,
                    boot_key: OtaBoot.SWITCH_BOOT,
                    finalize_key: self._finalize_update,
                },
                failure_key: {
                    state_key: OtaStatus.UPDATE_FAIL_STATE,
                    boot_key: OtaBoot.SWITCH_BOOT_FAIL,
                },
            },
            OtaStatus.SWITCHB_STATE: {
                status_checker_key: self._confirm_bankb,
                success_key: {
                    state_key: OtaStatus.NORMAL_STATE,
                    boot_key: OtaBoot.SWITCH_BOOT,
                    finalize_key: self._finalize_update,
                },
                failure_key: {
                    state_key: OtaStatus.UPDATE_FAIL_STATE,
                    boot_key: OtaBoot.SWITCH_BOOT_FAIL,
                },
            },
            OtaStatus.ROLLBACKA_STATE: {
                status_checker_key: self._confirm_banka,
                success_key: {
                    state_key: OtaStatus.NORMAL_STATE,
                    boot_key: OtaBoot.ROLLBACK_BOOT,
                    finalize_key: self._finalize_rollback,
                },
                failure_key: {
                    state_key: OtaStatus.ROLLBACK_FAIL_STATE,
                    boot_key: OtaBoot.ROLLBACK_BOOT_FAIL,
                },
            },
            OtaStatus.ROLLBACKB_STATE: {
                status_checker_key: self._confirm_bankb,
                success_key: {
                    state_key: OtaStatus.NORMAL_STATE,
                    boot_key: OtaBoot.ROLLBACK_BOOT,
                    finalize_key: self._finalize_rollback,
                },
                failure_key: {
                    state_key: OtaStatus.ROLLBACK_FAIL_STATE,
                    boot_key: OtaBoot.ROLLBACK_BOOT_FAIL,
                },
            },
            OtaStatus.UPDATE_STATE: {
                success_key: {
                    state_key: OtaStatus.UPDATE_FAIL_STATE,
                    boot_key: OtaBoot.UPDATE_INCOMPLETE,
                },
            },
            OtaStatus.PREPARED_STATE: {
                success_key: {
                    state_key: OtaStatus.UPDATE_FAIL_STATE,
                    boot_key: OtaBoot.UPDATE_INCOMPLETE,
                },
            },
            OtaStatus.ROLLBACK_STATE: {
                success_key: {
                    state_key: OtaStatus.ROLLBACK_FAIL_STATE,
                    boot_key: OtaBoot.ROLLBACK_INCOMPLETE,
                },
            },
            OtaStatus.UPDATE_FAIL_STATE: {
                success_key: {
                    state_key: OtaStatus.NORMAL_STATE,
                    boot_key: OtaBoot.NORMAL_BOOT,
                },
            },
        }

        if (
            state_table[status].get(status_checker_key) is None
            or state_table[status][status_checker_key]()
        ):
            # check OK
            checker_result_key = success_key
        else:
            # check NG
            checker_result_key = failure_key

        if state_table[status][checker_result_key].get(finalize_key) is not None:
            # Do finalize update/rollback
            state_table[status][checker_result_key][finalize_key]()

        return (
            state_table[status][checker_result_key][boot_key],  # boot status
            state_table[status][checker_result_key][state_key],  # ecu status
        )
