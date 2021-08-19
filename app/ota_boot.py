#!/usr/bin/env python3

import os
import shutil

from ota_status import OtaStatus
from grub_control import GrubCtl
from constants import OtaBootConst
import configs as cfg

from logging import getLogger, INFO, DEBUG

logger = getLogger(__name__)
logger.setLevel(INFO)


class OtaBoot:
    """
    OTA Startup class
    """
    _grub_config_file = cfg.GRUB_CFG_FILE
    _ecuinfo_yaml_file = cfg.ECUINFO_YAML_FILE

    def __init__(self):
        """
        Initialize
        """
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
        src_file = self._ecuinfo_yaml_file + ".update"
        dest_file = self._ecuinfo_yaml_file
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

    # TODO: better way to implement?
    def _boot(self, status):
        """
        OTA boot
        """
        state_table = OtaBootConst.state_table
        function_table = {
            "_confirm_banka" : self._confirm_banka,
            "_confirm_bankb" : self._confirm_bankb,
            "_finalize_update" : self._finalize_update,
            "_finalize_rollback" : self._finalize_rollback,
        }

        if (
            OtaBootConst.status_checker_key not in state_table[status]
            or function_table[state_table[status][OtaBootConst.status_checker_key]]()
        ):
            # check OK
            checker_result_key = OtaBootConst.success_key
        else:
            # check NG
            checker_result_key = OtaBootConst.failure_key

        if (
            OtaBootConst.finalize_key in state_table[status]
            and state_table[status][checker_result_key][OtaBootConst.finalize_key] in function_table
            ):
            # Do finalize update/rollback
            function_table[state_table[status][checker_result_key][OtaBootConst.finalize_key]]()

        return (
            state_table[status][checker_result_key][OtaBootConst.boot_key],  # boot status
            state_table[status][checker_result_key][OtaBootConst.state_key],  # ecu status
        )
