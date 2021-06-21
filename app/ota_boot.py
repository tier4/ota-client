#!/usr/bin/env python3

import tempfile
import os
import shutil

from ota_status import OtaStatus
from grub_control import GrubCtl

from logging import getLogger, INFO, DEBUG

logger = getLogger(__name__)
logger.setLevel(INFO)

class OtaBoot:
    """
    OTA Startup class
    """

    def __init__(
        self,
        ota_status_file="/boot/ota/ota_status",
        default_grub_file="/etc/default/grub",
        grub_config_file="/boot/grub/grub.cfg",
        custom_config_file="/boot/grub/custom.cfg",
        bank_info_file="/boot/ota/bankinfo.yaml",
        fstab_file="/etc/fstab",
        ecuinfo_yaml_file="/boot/ota/ecuinfo.yaml",
    ):
        """
        Initialize
        """
        self._grub_cfg_file = grub_config_file
        self.__ecuinfo_yaml_file = ecuinfo_yaml_file
        # status exist check
        if not os.path.exists(ota_status_file):
            self._gen_ota_status_file(ota_status_file)
        self._ota_status = OtaStatus(ota_status_file=ota_status_file)
        self._grub_ctl = GrubCtl(
            default_grub_file=default_grub_file,
            grub_config_file=grub_config_file,
            custom_config_file=custom_config_file,
            bank_info_file=bank_info_file,
            fstab_file=fstab_file,
        )

    def _gen_ota_status_file(self, ota_status_file):
        """
        generate OTA status file
        """
        with tempfile.NamedTemporaryFile(delete=False) as ftmp:
            tmp_file = ftmp.name
            with open(ftmp.name, "w") as f:
                f.write("NORMAL")
                f.flush()
        os.sync()
        dir_name = os.path.dirname(ota_status_file)
        if not os.path.exists(dir_name):
            os.makedirs(dir_name)
        shutil.move(tmp_file, ota_status_file)
        logger.info(f"{ota_status_file}  generated.")
        os.sync()
        return True

    def _error_nortify(self, err_str):
        """
        Error nortify (stub)
        """
        logger.debug(f"Error Nortify: {err_str}")

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
                logger.debug(f"file move: {src_file} to {dest_file}")
                shutil.move(src_file, dest_file)
        else:
            logger.error(f"file not found: {src_file}")
            return False
        return True

    def _boot(self, noexec=False):
        """
        OTA boot
        """
        result = ""
        # get status
        status = self._ota_status.get_ota_status()
        logger.debug(f"Status: {status}")

        if status == "NORMAL":
            # normal boot
            logger.debug("OTA normal boot!")
            result = "NORMAL_BOOT"
        elif status == "SWITCHA":
            # boot switching B to A bank
            logger.debug("OTA switch to A bank boot")
            if self._confirm_banka():
                # regenerate 'grub.cfg' file
                if noexec:
                    logger.debug("NOEXRC regenerate grub.cfg")
                    logger.debug("NOEXRC delete custum.cfg")
                else:
                    logger.debug("regenerate grub.cfg")
                    logger.debug("delete custum.cfg")
                    self._update_finalize_ecuinfo_file()
                    self._grub_ctl.re_generate_grub_config()
                    self._grub_ctl.delete_custom_cfg_file()
                    self._ota_status.inc_rollback_count()
                result = "SWITCH_BOOT"
            else:
                err_str = "Error: OTA switch to A Bank boot error!"
                # rollback
                if noexec:
                    logger.debug("NOEXRC delete custom.cfg")
                else:
                    logger.debug("delete custom.cfg")
                    self._grub_ctr.delete_custom_cfg_file()
                self._error_nortify(err_str)
            # set to normal status
            self._ota_status.set_ota_status("NORMAL")
            result = "SWITCH_BOOT"
        elif status == "SWITCHB":
            # boot switching A to B bank
            logger.debug("OTA switch to B Bank boot")
            if self._confirm_bankb():
                # regenerate 'grub.cfg' file
                if noexec:
                    logger.debug("NOEXRC regenerate grub.cfg")
                    logger.debug("NOEXRC delete custum.cfg")
                else:
                    logger.debug("regenerate grub.cfg")
                    logger.debug("delete custum.cfg")
                    self._update_finalize_ecuinfo_file()
                    self._grub_ctl.re_generate_grub_config()
                    self._grub_ctl.delete_custom_cfg_file()
                    self._ota_status.inc_rollback_count()
                result = "SWITCH_BOOT"
            else:
                err_str = "Error: OTA switch to B Bank boot error!"
                # rollback
                if noexec:
                    logger.debug("NOEXRC delete custom.cfg")
                else:
                    logger.debug("delete custom.cfg")
                    self._grub_ctl.delete_custom_cfg_file()
                self._error_nortify(err_str)
                result = "SWITCH_BOOT_FAIL"
            # set to normal status
            self._ota_status.set_ota_status("NORMAL")
        elif status == "ROLLBACKA":
            logger.debug("OTA rollback to A Bank boot")
            if self._confirm_banka():
                logger.debug("bank A OK")
                result = "ROLLBACK_BOOT"
            else:
                err_str = "Error: OTA rollback to A Bank boot error!"
                # rollback
                self._error_nortify(err_str)
                result = "ROLLBACK_BOOT_FAIL"
        elif status == "ROLLBACKB":
            logger.debug("OTA rollback to B Bank boot")
            if self._confirm_bankb():
                logger.debug("bank B OK")
                result = "ROLLBACK_BOOT"
            else:
                err_str = "Error: OTA rollback to B Bank boot error!"
                # rollback
                self._error_nortify(err_str)
                result = "ROLLBACK_BOOT_FAIL"
        elif status == "ROLLBACK":
            logger.debug("Rollback imcomplete!")
            # status error!
            err_str = "OTA Status error: " + status
            self._error_nortify(err_str)
            result = "ROLLBACK_BOOT_FAIL"
            # set to normal status
            # toDo : clean up '/boot'
            self._ota_status.set_ota_status("NORMAL")
        else:
            # status error!
            logger.error(f"OTA status error: {status}")
            err_str = "OTA Status error: " + status
            self._error_nortify(err_str)
            result = "UPDATE_IMCOMPLETE"
            # set to normal status
            # toDo : clean up '/boot'
            self._ota_status.set_ota_status("NORMAL")

        return result


if __name__ == "__main__":
    """
    OTA boot main
    """
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--noexec", help="No file handling execution.", default=False)

    args = parser.parse_args()
    # otaboot = OtaBoot(ota_status_file='tests/ota_status', grub_config_file=args.input_file, custom_config_file = 'test/custom.cfg', bank_info_file = 'test/bankinfo.yaml')
    otaboot = OtaBoot()
    result = otaboot._boot(noexec=args.noexec)
    print("boot result:", result)
