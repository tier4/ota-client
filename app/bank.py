#!/usr/bin/env python3

from pathlib import Path
import tempfile
import shutil
import shlex
import subprocess
import yaml
import re
from copy import deepcopy

import configs as cfg
from logging import getLogger, INFO, DEBUG

logger = getLogger(__name__)
logger.setLevel(cfg.LOG_LEVEL_TABLE.get(__name__, INFO))


def _blkid_command(device=None):
    command_line = "blkid" if device is None else f"blkid {device}"
    return subprocess.check_output(shlex.split(command_line))


def _get_ext4_blks(device=None):
    blkid = _blkid_command(device)

    blks = []
    for blk in blkid.split(b"\n"):
        match = re.match(r'(.*):\s+UUID="([a-f0-9-]*)"\s+TYPE="ext4"', blk.decode())
        if match:
            ext4 = {}
            ext4["DEV"] = match.group(1)
            ext4["UUID"] = match.group(2)
            blks.append(ext4)
    return blks


def _get_uuid_from_blkid(bank):
    """
    get bank device uuid by the 'blkid' command
    """
    return _get_ext4_blks(device=bank)[0]["UUID"]


def _get_current_devfile_by_fstab(fstab_file):
    """"""
    with open(fstab_file, "r") as f:
        while True:
            line = f.readline()
            if not line:
                break
            match = re.match(
                r"^(?!#)(\/dev\/disk\/by-uuid\/|UUID=)([a-f0-9-]*)\s+(.*?)\s+", line
            )
            if not match:
                continue
            if match.group(3) == "/":
                root_uuid = match.group(2)
                root_devfile = Path(f"/dev/disk/by-uuid/{match.group(2)}").resolve()
                logger.debug(f"root_uuid: {root_uuid}, root_devfile: {root_devfile}")
            elif match.group(3) == "/boot":
                boot_uuid = match.group(2)
                boot_devfile = Path(f"/dev/disk/by-uuid/{match.group(2)}").resolve()
                logger.debug(f"boot_uuid: {boot_uuid}, boot_devfile: {boot_devfile}")
    # NOTE: if all root_devfile, root_uuid, boot_devfile and boot_uuid are not set,
    # the following line raises exception.
    return root_devfile, root_uuid, boot_devfile, boot_uuid


def _gen_bankinfo_file(bank_info_file: Path, fstab_file: Path):
    """
    generate the bank information file
    """
    (
        root_devfile,
        root_uuid,
        boot_devfile,
        boot_uuid,
    ) = _get_current_devfile_by_fstab(fstab_file)
    blks = _get_ext4_blks()
    stby_devfile = ""
    for blk in blks:
        if blk["DEV"] == root_devfile or blk["UUID"] == root_uuid:
            logger.debug(f"root dev: {root_devfile} {root_uuid}")
        elif blk["DEV"] == boot_devfile or blk["UUID"] == boot_uuid:
            logger.debug(f"boot dev: {boot_devfile} {boot_uuid}")
        else:
            logger.debug(f"another bank: {blk}")
            stby_devfile = blk["DEV"]
            stby_uuid = blk["UUID"]
            break
    if boot_devfile == "" or root_devfile == "" or stby_devfile == "":
        logger.error("device info error!")
        logger.info(f"root: {root_devfile} boot: {boot_devfile} stby: {stby_devfile}")
        return False
    else:
        with tempfile.NamedTemporaryFile("w", delete=False, prefix=__name__) as f:
            tmp_file = f.name
            f.write("banka: " + str(root_devfile) + "\n")
            f.write("bankb: " + str(stby_devfile) + "\n")
            logger.debug(f"banka: {root_devfile}")
            logger.debug(f"bankb: {stby_devfile}")

        bank_info_file.parent.mkdir(exist_ok=True)
        shutil.move(tmp_file, bank_info_file)
    return True


def _get_bank_info(ota_config_file: Path):
    """
    get bank information
    """
    banka = ""
    bankb = ""
    try:
        with open(ota_config_file, "r") as fyml:
            logger.debug(f"open: {ota_config_file}")
            ota_config = yaml.load(fyml, Loader=yaml.SafeLoader)
            banka = ota_config["banka"]
            bankb = ota_config["bankb"]
            logger.debug(f"banka: {banka} bankb: {bankb}")
    except:
        logger.exception("Cannot get bank infomation!:")
    return banka, bankb


class _baseBankInfo:
    _bank_info_file: Path = cfg.BANK_INFO_FILE
    _fstab_file: Path = cfg.FSTAB_FILE

    def __init__(self):
        if not self._bank_info_file.is_file():
            _gen_bankinfo_file(self._bank_info_file, self._fstab_file)
        self.bank_a, self.bank_b = _get_bank_info(self._bank_info_file)
        self.bank_a_uuid = _get_uuid_from_blkid(self.bank_a)
        self.bank_b_uuid = _get_uuid_from_blkid(self.bank_b)

    def get_banka(self):
        """
        Get bank A
        """
        return self.bank_a

    def get_banka_uuid(self):
        """
        Get bank A UUID
        """
        return self.bank_a_uuid

    def is_banka(self, bank):
        """
        Is bank A
        """
        return bank == self.bank_a

    def is_banka_uuid(self, bank_uuid):
        """
        Is bank A UUID
        """
        return bank_uuid == self.bank_a_uuid

    def get_bankb(self):
        """
        Get bank B
        """
        return self.bank_b

    def get_bankb_uuid(self):
        """
        Get bank B UUID
        """
        return self.bank_b_uuid

    def is_bankb(self, bank):
        """
        Is bank B
        """
        return bank == self.bank_b

    def is_bankb_uuid(self, bank_uuid):
        """
        Is bank B UUID
        """
        return bank_uuid == self.bank_b_uuid


class BankInfo(_baseBankInfo):
    """
    OTA Bank device info class
    """

    def __init__(self):
        # init current bank status
        super().__init__()
        self._setup_current_next_root_dev()

    def _setup_current_next_root_dev(self):
        """
        setup the current/next root device from '/etc/fstab'
        """
        with open(self._fstab_file, "r") as f:
            lines = f.readlines()

        for l in lines:
            if l[0] == "#":
                continue

            fstab_list = l.split()
            if fstab_list[1] == "/":
                # root mount line
                logger.debug(f"root found: {fstab_list[1]}")
                if fstab_list[0].find("UUID=") == 0:
                    # UUID type definition
                    _current_bank_uuid_str = fstab_list[0]
                    if _current_bank_uuid_str.find(self.bank_a_uuid) >= 0:
                        # current bank is A
                        _current_bank = self.bank_a
                        _next_bank = self.bank_b
                        _next_bank_uuid_str = "UUID=" + self.bank_b_uuid
                    elif _current_bank_uuid_str.find(self.bank_b_uuid) >= 0:
                        # current bank is B
                        _current_bank = self.bank_b
                        _next_bank = self.bank_a
                        _next_bank_uuid_str = "UUID=" + self.bank_a_uuid
                    else:
                        # current bank is another bank
                        logger.error("current bank is not banka or bankb!")
                        raise Exception("failed to parse fstab file.")
                elif fstab_list[0].find("/dev/disk/by-uuid/") == 0:
                    # by-uuid device file
                    _current_bank_uuid_str = fstab_list[0]
                    if _current_bank_uuid_str.find(self.bank_a_uuid) >= 0:
                        # current bank is A
                        _current_bank = self.bank_a
                        _next_bank = self.bank_b
                        _next_bank_uuid_str = (
                            "/dev/disk/by-uuid/" + self.bank_b_uuid
                        )
                    elif _current_bank_uuid_str.find(self.bank_b_uuid) >= 0:
                        # current bank is B
                        _current_bank = self.bank_b
                        _next_bank = self.bank_a
                        _next_bank_uuid_str = (
                            "/dev/disk/by-uuid/" + self.bank_a_uuid
                        )
                    else:
                        logger.error("current bank is not banka or bankb!")
                        raise Exception("failed to parse fstab file.")
                else:
                    # device file name
                    _current_bank = fstab_list[0]
                    if _current_bank == self.bank_a:
                        _current_bank_uuid_str = "UUID=" + self.bank_a_uuid
                        _next_bank = self.bank_b
                        _next_bank_uuid_str = "UUID" + self.bank_b_uuid
                    elif _current_bank == self.bank_b:
                        _current_bank_uuid_str = "UUID=" + self.bank_b_uuid
                        _next_bank = self.bank_a
                        _next_bank_uuid_str = "UUID=" + self.bank_a_uuid
                    else:
                        logger.error("current bank is not banka or bankb!")
                        raise Exception("failed to parse fstab file.")

        (
            self._current_bank,
            self._current_bank_uuid_str,
            self._next_bank,
            self._next_bank_uuid_str,
        ) = (_current_bank, _current_bank_uuid_str, _next_bank, _next_bank_uuid_str)

    def get_current_bank(self):
        """
        Get current bank devoice file
        """
        return self._current_bank

    def get_current_bank_uuid(self):
        """
        Get current bank UUID
        """
        if self.is_banka(self._current_bank):
            return self.bank_a_uuid
        elif self.is_bankb(self._current_bank):
            return self.bank_b_uuid
        return ""

    def get_current_bank_uuid_str(self):
        """
        Get current bank UUID string
        """
        return self._current_bank_uuid_str

    def is_current_bank(self, bank_str):
        """
        Confirm the current bank
        """
        return self._current_bank == bank_str

    def get_next_bank(self):
        """
        Get next bank
        """
        return self._next_bank

    def get_next_bank_uuid(self):
        """
        Get next bank UUID
        """
        if self.is_banka(self._next_bank):
            return self.bank_a_uuid
        elif self.is_bankb(self._next_bank):
            return self.bank_b_uuid
        return ""

    def get_next_bank_uuid_str(self):
        """
        Get the next bank UUID string
        """
        logger.info(f"next_bank_uuid: {self._next_bank_uuid_str}")
        return self._next_bank_uuid_str

    def export(self):
        return deepcopy(self)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--bankinfo", help="bank info file path name", default="/boot/ota/bankinfo.yaml"
    )
    parser.add_argument("--fstab", help="fstab file path name", default="/etc/fstab")

    args = parser.parse_args()

    root_dev, root_uuid, boot_dev, boot_uuid = _get_current_devfile_by_fstab(args.fstab)
    print("root dev: ", root_dev)
    print("root uuid: ", root_uuid)
    print("boot dev: ", boot_dev)
    print("boot uuid: ", boot_uuid)

    cfg.FSTAB_FILE = args.fstab
    cfg.BANK_INFO_FILE = args.bankinfo
    bank_info = BankInfo()
    print("bank a: ", bank_info.bank_a)
    print("bank a uuid:", bank_info.bank_a_uuid)

    print("bank b: ", bank_info.bank_b)
    print("bank b uuid: ", bank_info.bank_b_uuid)

    print("current bank: ", bank_info._current_bank)
    print("current bank uuid: ", bank_info._current_bank_uuid_str)

    print("next bank: ", bank_info._next_bank)
    print("next bank uuid: ", bank_info._next_bank_uuid_str)
