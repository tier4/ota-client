#!/usr/bin/env python3

import tempfile
import os
import shutil
import subprocess
import argparse

from ota_status import OtaStatus
from grub_control import GrubCtl


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
        """"""
        self.__verbose = True
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
        print(ota_status_file, " generated.")
        os.sync()
        return True

    def _error_nortify(self, err_str):
        """
        Error nortify (stub)
        """
        print("Error Nortify: " + err_str)

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
                if self.__verbose:
                    print("file move: ", src_file, " to ", dest_file)
                shutil.move(src_file, dest_file)
        else:
            print("file not found: ", src_file)
            return False
        return True

    def _boot(self, noexec=False):
        """
        OTA boot
        """
        result = ""
        # get status
        status = self._ota_status.get_ota_status()
        if self.__verbose:
            print("Status: " + status)

        if status == "NORMAL":
            # normal boot
            if self.__verbose:
                print("OTA normal boot!")
            result = "NORMAL_BOOT"
        elif status == "SWITCHA":
            # boot switching B to A bank
            if self.__verbose:
                print("OTA switch to A bank boot")
            if self._confirm_banka():
                # regenerate 'grub.cfg' file
                if noexec:
                    if self.__verbose:
                        print("NOEXRC regenerate grub.cfg")
                        print("NOEXRC delete custum.cfg")
                else:
                    if self.__verbose:
                        print("regenerate grub.cfg")
                        print("delete custum.cfg")
                    self._update_finalize_ecuinfo_file()
                    self._grub_ctl.re_generate_grub_config()
                    self._grub_ctl.delete_custom_cfg_file()
                    self._ota_status.inc_rollback_count()
                result = "SWITCH_BOOT"
            else:
                err_str = "Error: OTA switch to A Bank boot error!"
                # rollback
                if noexec:
                    if self.__verbose:
                        print("NOEXRC delete custom.cfg")
                else:
                    if self.__verbose:
                        print("delete custom.cfg")
                    self._grub_ctr.delete_custom_cfg_file()
                self._error_nortify(err_str)
            # set to normal status
            self._ota_status.set_ota_status("NORMAL")
            result = "SWITCH_BOOT"
        elif status == "SWITCHB":
            # boot switching A to B bank
            if self.__verbose:
                print("OTA switch to B Bank boot")
            if self._confirm_bankb():
                # regenerate 'grub.cfg' file
                if noexec:
                    if self.__verbose:
                        print("NOEXRC regenerate grub.cfg")
                        print("NOEXRC delete custum.cfg")
                else:
                    if self.__verbose:
                        print("regenerate grub.cfg")
                        print("delete custum.cfg")
                    self._update_finalize_ecuinfo_file()
                    self._grub_ctl.re_generate_grub_config()
                    self._grub_ctl.delete_custom_cfg_file()
                    self._ota_status.inc_rollback_count()
                result = "SWITCH_BOOT"
            else:
                err_str = "Error: OTA switch to B Bank boot error!"
                # rollback
                if noexec:
                    if self.__verbose:
                        print("NOEXRC delete custom.cfg")
                else:
                    if self.__verbose:
                        print("delete custom.cfg")
                    self._grub_ctl.delete_custom_cfg_file()
                self._error_nortify(err_str)
                result = "SWITCH_BOOT_FAIL"
            # set to normal status
            self._ota_status.set_ota_status("NORMAL")
        elif status == "ROLLBACKA":
            if self.__verbose:
                print("OTA rollback to A Bank boot")
            if self._confirm_banka():
                if self.__verbose:
                    print("bank A OK")
                result = "ROLLBACK_BOOT"
            else:
                err_str = "Error: OTA rollback to A Bank boot error!"
                # rollback
                self._error_nortify(err_str)
                result = "ROLLBACK_BOOT_FAIL"
        elif status == "ROLLBACKB":
            if self.__verbose:
                print("OTA rollback to B Bank boot")
            if self._confirm_bankb():
                if self.__verbose:
                    print("bank B OK")
                result = "ROLLBACK_BOOT"
            else:
                err_str = "Error: OTA rollback to B Bank boot error!"
                # rollback
                self._error_nortify(err_str)
                result = "ROLLBACK_BOOT_FAIL"
        elif status == "ROLLBACK":
            if self.__verbose:
                print("Rollback imcomplete!")
            # status error!
            err_str = "OTA Status error: " + status
            self._error_nortify(err_str)
            result = "ROLLBACK_BOOT_FAIL"
            # set to normal status
            # toDo : clean up '/boot'
            self._ota_status.set_ota_status("NORMAL")
        else:
            # status error!
            print("OTA status error: ", status)
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--noexec", help="No file handling execution.", default=False)

    args = parser.parse_args()
    # otaboot = OtaBoot(ota_status_file='tests/ota_status', grub_config_file=args.input_file, custom_config_file = 'test/custom.cfg', bank_info_file = 'test/bankinfo.yaml')
    otaboot = OtaBoot()
    result = otaboot._boot(noexec=args.noexec)
    print("boot result:", result)
