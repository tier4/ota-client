#!/usr/bin/env python3

import tempfile
import re
import os
import shlex
import shutil
import subprocess
from bank import BankInfo

from logging import getLogger, INFO, DEBUG

logger = getLogger(__name__)
logger.setLevel(INFO)

class GrubCtl:
    """
    OTA GRUB control class
    """

    def __init__(
        self,
        default_grub_file="/etc/default/grub",
        grub_config_file="/boot/grub/grub.cfg",
        custom_config_file="/boot/grub/custom.cfg",
        bank_info_file="/boot/ota/bankinfo.yaml",
        fstab_file="/etc/fstab",
    ):
        """"""
        self._bank_info = BankInfo(bank_info_file=bank_info_file, fstab_file=fstab_file)
        self._grub_cfg_file = grub_config_file
        self._custom_cfg_file = custom_config_file
        self._default_grub_file = default_grub_file

    def get_bank_info(self):
        return self._bank_info

    def change_to_next_bank(self, config_file):
        """
        change the custum configuration menu root partition device
        """
        if not os.path.exists(config_file):
            logger.warning(f"File not exist: {config_file}")
            return False
        # get bank info
        current_bank = self._bank_info.get_current_bank()
        current_bank_uuid = self._bank_info.get_current_bank_uuid()
        next_bank = self._bank_info.get_next_bank()
        next_bank_uuid = self._bank_info.get_next_bank_uuid()
        logger.debug("geberate temp file!")
        linux_key = "linux"
        root_prefix = "root="
        root_uuid_prefix = "root=UUID="
        with tempfile.NamedTemporaryFile(delete=False) as ftmp:
            tmp_file = ftmp.name
            logger.debug(f"temp file: {ftmp.name}")
            with open(tmp_file, mode="w") as f:
                logger.debug("temp file open!")
                with open(config_file, mode="r") as fcustom:
                    logger.debug(f"custum config file open: {config_file}")
                    # read lines from custum config file
                    lines = fcustom.readlines()
                    for l in lines:
                        # find linux
                        if l.find(linux_key) >= 0:
                            # find root string
                            if l.find(root_prefix) >= 0:
                                # found root=xxxxx
                                if l.find(root_uuid_prefix) >= 0:
                                    # root uuid
                                    logger.debug(f"ORG: {l}")
                                    if l.find(current_bank_uuid) >= 0:
                                        # replace to next bank root
                                        lrep = l.replace(
                                            current_bank_uuid, next_bank_uuid
                                        )
                                        logger.debug(f"RPL: {lrep}")
                                    elif l.find(next_bank_uuid):
                                        logger.debug("No replace!")
                                        lrep = l
                                    else:
                                        logger.error(f"root partition missmatch! : {l}")
                                        logger.debug(f"current uuid: {current_bank_uuid}")
                                        logger.debug(f"next uuid: {next_bank_uuid}")
                                        return False
                                else:
                                    # root dev file
                                    logger.debug(f"ORG: {l}")
                                    if l.find(current_bank) >= 0:
                                        # replace to next bank root
                                        lrep = l.replace(current_bank, next_bank)
                                        logger.debug(f"RPL: {lrep}")
                                        logger.debug(f"current: {current_bank}")
                                        logger.debug(f"next: {next_bank}")
                                    elif l.find(next_bank) >= 0:
                                        # no need to replace
                                        logger.debug("No need to replace!")
                                        lrep = l
                                    else:
                                        logger.error(f"root partition missmatch! {l}")
                                        return False
                                f.write(lrep)
                            else:
                                f.write(l)
                        else:
                            f.write(l)
                    f.flush()
        if os.path.exists(config_file):
            # backup
            shutil.copy(config_file, config_file + ".old")
        # mv tmp file to custom config file
        shutil.move(tmp_file, config_file)
        return True

    def make_grub_custom_configuration_file(self, input_file, output_file):
        """
        generate the custom configuration file for the another bank boot.
        """
        # input_file = self._grub_cfg_file
        logger.debug(f"input_file: {input_file}")
        logger.debug(f"output_file: {output_file}")

        if not os.path.exists(input_file):
            logger.info(f"No input file: {input_file}")
            return

        # output_file = self._custom_cfg_file

        banka_uuid = self._bank_info.get_banka_uuid()
        bankb_uuid = self._bank_info.get_bankb_uuid()

        menuentry_start = "menuentry "
        menuentry_end = "}"

        linux_root_re = r"linux.+root="
        root_device_uuid_str = "root=UUID=" + self._bank_info.get_current_bank_uuid()
        root_device_str = "root=" + self._bank_info.get_current_bank()

        found_target = False
        with tempfile.NamedTemporaryFile(delete=False) as ftmp:
            tmp_file = ftmp.name
            logger.debug(f"tmp file: {ftmp.name}")
            with open(ftmp.name, mode="w") as fout:
                with open(input_file, mode="r") as fin:
                    logger.debug(f"{input_file} opened!")
                    menu_writing = False
                    menu_count = 0
                    lines = fin.readlines()
                    for l in lines:
                        if menu_writing:
                            fout.write(l)
                            # check menuentry end
                            if 0 <= l.find(menuentry_end):
                                logger.debug("menu writing end!")
                                menu_writing = False
                                break
                            match = re.search(linux_root_re, l)
                            if match:
                                logger.debug(f"linux root match: {l}")
                                # check root
                                logger.debug(f"root uuid: {root_device_uuid_str}")
                                logger.debug(f"root devf: {root_device_str}")
                                if l.find(root_device_uuid_str) >= 0:
                                    logger.debug(f"found target: {l}")
                                    found_target = True
                                elif l.find(root_device_str) >= 0:
                                    logger.debug(f"found target: {l}")
                                    found_target = True
                        else:
                            # check menuentry start
                            if found_target:
                                break
                            if 0 == l.find(menuentry_start):
                                logger.debug(f"menuentry found! : {l}")
                                menu_writing = True
                                menu_count += 1
                                fout.write(l)

        if not found_target:
            logger.error("No menu entry found!")
            return False
        try:
            # change root partition
            self.change_to_next_bank(tmp_file)
        except Exception as e:
            logger.error("Change next bank error:")
            return False

        if os.path.exists(output_file):
            # backup
            shutil.copy(output_file, output_file + ".old")
        # mv tmp file to custom config file
        shutil.move(tmp_file, output_file)
        return True

    def grub_configuration(self, style_str="menu", timeout=10):
        """
        Grub configuration setup:
            GRUB_TIMEOUT_STYLE=menu
            GRUB_TIMEOUT=10
        """

        with tempfile.NamedTemporaryFile(delete=False) as ftmp:
            temp_file = ftmp.name
            logger.debug(f"tem file: {ftmp.name}")
            with open(ftmp.name, mode="w") as f:
                with open(self._default_grub_file, mode="r") as fgrub:
                    lines = fgrub.readlines()
                    for l in lines:
                        pos = l.find("GRUB_TIMEOUT_STYLE=")
                        if pos >= 0:
                            f.write(l[pos : (pos + 19)] + style_str)
                            continue

                        pos = l.find("GRUB_TIMEOUT=")
                        if pos >= 0:
                            f.write(l[pos : (pos + 13)] + str(timeout))
                            continue

                        # pos = l.find("GRUB_DISABLE_LINUX_UUID=")
                        # if(pos >= 0):
                        #    f.write(l[pos:(pos + 24)] + disable_uuid_str)
                        #    continue

                        f.write(l)
                        f.flush()

            # move temp to grub
            os.sync()
            if os.path.exists(self._default_grub_file):
                if os.path.exists(self._default_grub_file + ".old"):
                    os.remove(self._default_grub_file + ".old")
                shutil.copy2(self._default_grub_file, self._default_grub_file + ".old")
            shutil.move(temp_file, self._default_grub_file)
        return True

    def make_grub_configuration_file(self, output_file):
        """
        make the "grub.cfg" file
        """
        command_line = "grub-mkconfig"

        try:
            with tempfile.NamedTemporaryFile(delete=False) as ftmp:
                tmp_file = ftmp.name
                with open(tmp_file, mode="w") as f:
                    logger.debug(f"tmp file opened!: {ftmp.name}")
                    res = subprocess.check_call(shlex.split(command_line), stdout=f)
                # move temp to grub.cfg
                if os.path.exists(output_file):
                    if os.path.exists(output_file + ".old"):
                        os.remove(output_file + ".old")
                    shutil.copy2(output_file, output_file + ".old")
                shutil.move(tmp_file, output_file)
        except:
            logger.exception("failed genetrating grub.cfg")
            return False
        return True

    def re_generate_grub_config(self):
        """
        regenarate the grub config file
        """
        # change the grub genaration configuration
        self.grub_configuration()

        # make the grub configuration file
        res = self.make_grub_configuration_file(self._grub_cfg_file)
        return res

    def count_grub_menue_entries_wo_submenu(self, input_file):
        """
        count the grub menu entries without submenue
        """
        menuentry_str = "menuentry "
        submenu_str = "submenu "
        menu_entries = 0

        if os.path.exists(input_file):
            with open(input_file, "r") as f:
                lines = f.readlines()
                for l in lines:
                    pos = l.find(menuentry_str)
                    if pos == 0:
                        logger.debug(f"{menu_entries} : {l}")
                        menu_entries += 1
                    pos = l.find(submenu_str)
                    if pos == 0:
                        logger.debug(f"{menu_entries} : {l}")
                        menu_entries += 1
        else:
            logger.warning(f"file not exist : {input_file}")
            menu_entries = -1
        logger.debug(f"entries: {menu_entries}")
        return menu_entries

    def count_grub_menue_entries(self, input_file):
        """
        count the grub menu entries
        """
        menuentry_str = "menuentry "
        menu_entries = 0

        if os.path.exists(input_file):
            with open(input_file, "r") as f:
                lines = f.readlines()
                for l in lines:
                    pos = l.find(menuentry_str)
                    if pos >= 0:
                        logger.debug(f"{menu_entries} : {l}")
                        menu_entries += 1
        else:
            logger.warning(f"file not exist! : {input_file}")
            menu_entries = -1

        logger.debug(f"entries: {menu_entries}")
        return menu_entries

    def set_next_boot_entry(self, menuentry_no):
        """
        set next boot grub menue entry to custom config menu
        """
        command_line = "grub-reboot " + str(menuentry_no)
        try:
            logger.debug(f"Do: subproxess.check_call({command_line})")
            res = subprocess.check_call(shlex.split(command_line))
        except:
            logger.exception("grub-setreboot error!")
            return False
        return True

    def set_next_bank_boot(self, no_submenu=True):
        """
        set next boot grub menue entry to custom config menu
        """
        # get grub.cfg menuentries
        if no_submenu:
            menu_entries = self.count_grub_menue_entries_wo_submenu(self._grub_cfg_file)
        else:
            menu_entries = self.count_grub_menue_entries(self._grub_cfg_file)
        if menu_entries > 0:
            # set next boot menuentry to custum menuentry
            res = self.set_next_boot_entry(menu_entries)
        else:
            logger.error("No grub entry in the grub.cfg file!")
            return False
        return res

    @staticmethod
    def delete_custom_cfg_file():
        """
        delete custom.cfg file
        """
        target_file = "/boot/grub/custom.cfg"
        os.remove(target_file)

    @staticmethod
    def reboot():
        """
        reboot
        """
        command_line = "reboot"
        try:
            res = subprocess.check_call(shlex.split(command_line))
        except:
            logger.error("reboot error!")
            return False
        return True

    def prepare_grub_switching_reboot(self):
        """
        prepare for GRUB control reboot for switching to another bank
        """
        # make custum.cfg file
        res = self.make_grub_custom_configuration_file(
            self._grub_cfg_file, self._custom_cfg_file
        )

        if res:
            # set next boot menu
            res = self.set_next_bank_boot()
        else:
            return False
        return res

    def grub_rollback_prepare(self):
        """
        GRUB data backup for rollback

        """
        # copy for rollback
        if os.path.exists(self._grub_cfg_file):
            shutil.copy2(self._grub_cfg_file, self._grub_cfg_file + ".rollback")
        else:
            logger.error("grub configuratiuion file not exist!")
            return False
        return True

    def grub_rollback_reboot(self):
        """
        GRUB rollback reboot
        """
        # ToDo: implement
        return True
