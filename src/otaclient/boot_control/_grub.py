# Copyright 2022 TIER IV, INC. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Implementation of grub boot control.

DEPRECATION WARNING(20231026):
    Current mechanism of defining and detecting slots is proved to be not robust.
    The design expects that rootfs device will always be sda, which might not be guaranteed
        as the sdx naming scheme is based on the order of kernel recognizing block devices.
        If rootfs somehow is not named as sda, the grub boot controller will fail to identifiy
        the slots and/or finding corresponding ota-partition files, finally failing the OTA.
    Also slots are detected by assuming the partition layout, which is less robust comparing to
        cboot and rpi_boot implementation of boot controller.

TODO(20231026): design new mechanism to define and manage slot.

NOTE(20231027) A workaround fix is applied to handle the edge case of rootfs not named as sda,
    Check GrubABPartitionDetector class for more details.
    This workaround only means to avoid OTA failed on edge condition and maintain backward compatibility,
    still expecting new mechanism to fundamentally resolve this issue.
"""


from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from pprint import pformat
from subprocess import CalledProcessError
from typing import ClassVar, Dict, List, NoReturn, Optional, Tuple

from otaclient import errors as ota_errors
from otaclient._types import OTAStatus
from otaclient.boot_control._slot_mnt_helper import SlotMountHelper
from otaclient.configs.cfg import cfg
from otaclient_common import cmdhelper
from otaclient_common._io import (
    read_str_from_file,
    symlink_atomic,
    write_str_to_file_atomic,
)
from otaclient_common.common import subprocess_call, subprocess_check_output

from ._ota_status_control import OTAStatusFilesControl, cat_proc_cmdline
from .configs import grub_cfg as boot_cfg
from .protocol import BootControllerProtocol

logger = logging.getLogger(__name__)


class _GrubBootControllerError(Exception):
    """Grub boot controller internal used exception."""


@dataclass
class _GrubMenuEntry:
    """
    NOTE: should only be called by the get_entry method

    linux: vmlinuz-<ver>
    linux_ver: <ver>
    initrd: initrd.img-<ver>
    """

    linux: str
    linux_ver: str
    menuentry: str
    initrd: str
    rootfs_uuid_str: str

    def __init__(self, ma: re.Match) -> None:
        """
        NOTE: check GrubHelper for capturing group definition
        """
        self.menuentry = ma.group("menu_entry")

        # get linux and initrd from menuentry
        if _linux := GrubHelper.linux_pa.search(self.menuentry):
            self.linux_ver = _linux.group("ver")
            self.linux = Path(_linux.group("kernel_path")).name
            self.rootfs_uuid_str = _linux.group("rootfs_uuid_str")
        if initrd_ma := GrubHelper.initrd_pa.search(self.menuentry):
            self.initrd = initrd_ma.group("initrd")

        assert self.linux and self.initrd, "failed to detect linux img and initrd"
        assert self.rootfs_uuid_str, "failed to detect rootfs_str"


class GrubHelper:
    menuentry_pa: ClassVar[re.Pattern] = re.compile(
        # whole capture group
        r"^(?P<menu_entry>\s*menuentry\s+"
        r"[^\{]*"  # menuentry options
        r"\{(?P<entry>[^\}]*)\}"  # menuentry block
        r")",  # end of whole capture
        re.MULTILINE | re.DOTALL,
    )
    linux_pa: ClassVar[re.Pattern] = re.compile(
        r"(?P<load_linux>^\s+linux\s+(?P<kernel_path>.*vmlinuz-(?P<ver>[\.\w\-]*)))"
        r"\s+(?P<cmdline>.*(?P<rootfs>root=(?P<rootfs_uuid_str>[^\s]*)).*)\s*$",
        re.MULTILINE,
    )
    rootfs_pa: ClassVar[re.Pattern] = re.compile(
        r"(?P<rootfs>root=(?P<rootfs_uuid_str>[^\s]*))"
    )

    initrd_pa: ClassVar[re.Pattern] = re.compile(
        r"^\s+initrd.*(?P<initrd>initrd.img-(?P<ver>[\.\w-]*))", re.MULTILINE
    )
    kernel_fname_pa: ClassVar[re.Pattern] = re.compile(r"^vmlinuz-(?P<ver>[\.\w-]*)$")

    VMLINUZ = "vmlinuz"
    INITRD = "initrd.img"
    FNAME_VER_SPLITTER = "-"
    SUFFIX_OTA = "ota"
    SUFFIX_OTA_STANDBY = "ota.standby"
    KERNEL_OTA = f"{VMLINUZ}{FNAME_VER_SPLITTER}{SUFFIX_OTA}"
    KERNEL_OTA_STANDBY = f"{VMLINUZ}{FNAME_VER_SPLITTER}{SUFFIX_OTA_STANDBY}"
    INITRD_OTA = f"{INITRD}{FNAME_VER_SPLITTER}{SUFFIX_OTA}"
    INITRD_OTA_STANDBY = f"{INITRD}{FNAME_VER_SPLITTER}{SUFFIX_OTA_STANDBY}"

    grub_default_options: ClassVar[Dict[str, str]] = {
        "GRUB_TIMEOUT_STYLE": "menu",
        "GRUB_TIMEOUT": "0",
        "GRUB_DISABLE_SUBMENU": "y",
        "GRUB_DISABLE_OS_PROBER": "true",
        "GRUB_DISABLE_RECOVERY": "true",
    }

    @classmethod
    def update_entry_rootfs(
        cls,
        grub_cfg: str,
        *,
        kernel_ver: str,
        rootfs_str: str,
        start: int = 0,
    ) -> Optional[str]:
        """Read in grub_cfg, update matched kernel entries' rootfs with <rootfs_str>,
            and then return the updated one.

        Params:
            grub_cfg: input grub_cfg str
            kernel_ver: kernel version str for the target entry
            rootfs_str: a str that indicates which rootfs device to use,
                like root=UUID=<uuid>
        """
        new_entry_block: Optional[str] = None
        entry_l, entry_r = None, None

        # loop over normal entry, find the target entry,
        # and then replace the rootfs string
        for entry in cls.menuentry_pa.finditer(grub_cfg, start):
            entry_l, entry_r = entry.span()
            entry_block = entry.group()
            # parse the entry block
            if (_linux := cls.linux_pa.search(entry_block)) and _linux.group(
                "ver"
            ) == kernel_ver:
                linux_line_l, linux_line_r = _linux.span()
                _new_linux_line, _count = cls.rootfs_pa.subn(rootfs_str, _linux.group())
                if _count == 1:
                    # replace rootfs string
                    new_entry_block = (
                        f"{entry_block[:linux_line_l]}"
                        f"{_new_linux_line}"
                        f"{entry_block[linux_line_r:]}"
                    )
                    break

        if new_entry_block is not None:
            updated_grub_cfg = (
                f"{grub_cfg[:entry_l]}{new_entry_block}{grub_cfg[entry_r:]}"
            )

            # keep finding next entry
            return cls.update_entry_rootfs(
                updated_grub_cfg,
                kernel_ver=kernel_ver,
                rootfs_str=rootfs_str,
                start=len(grub_cfg[:entry_l]) + len(new_entry_block),
            )
        else:  # no more new matched entry, return the input grub_cfg
            return grub_cfg

    @classmethod
    def get_entry(cls, grub_cfg: str, *, kernel_ver: str) -> Tuple[int, _GrubMenuEntry]:
        """Find the FIRST entry that matches the <kernel_ver>.

        NOTE: assume that the FIRST matching entry is the normal entry,
              which is correct in most cases(recovery entry will always
              be after the normal boot entry, and we by defautl disable
              recovery entry).
        """
        for index, entry_ma in enumerate(cls.menuentry_pa.finditer(grub_cfg)):
            if (
                _linux := cls.linux_pa.search(entry_ma.group())
            ) and kernel_ver == _linux.group("ver"):
                return index, _GrubMenuEntry(entry_ma)

        raise ValueError(f"requested entry for {kernel_ver} not found")

    @classmethod
    def update_grub_default(
        cls, grub_default: str, *, default_entry_idx: Optional[int] = None
    ) -> str:
        """Read in grub_default str and return updated one.

        Update rules:
        1. predefined default_kvp has the highest priority, and overrides any
           presented options in the original grub_default,
        2. option that specified multiple times will be merged into one,
           and the latest specified value will be used, or predefined default value will
           be used if such value defined.
        """
        default_kvp = cls.grub_default_options.copy()
        if default_entry_idx is not None:
            default_kvp["GRUB_DEFAULT"] = f"{default_entry_idx}"

        res_kvp: Dict[str, str] = {}
        for option_line in grub_default.splitlines():
            # NOTE: skip empty or commented lines
            if not option_line or option_line.startswith("#"):
                continue

            # NOTE(20230619): skip illegal option that is not in key=value form
            _raw_split = option_line.strip().split("=", maxsplit=1)
            if len(_raw_split) == 2:
                option_name, option_value = _raw_split[0].strip(), _raw_split[1].strip()
                res_kvp[option_name] = option_value

        # merge pre-set default value into the result
        # NOTE(20230830): pre-set default has the highest priority over
        #                 any already set options
        res_kvp.update(default_kvp)

        res = [
            (
                "# This file is generated by otaclient, "
                "modification might not be preserved across OTA."
            )
        ]
        res.extend(f"{k}={v}" for k, v in res_kvp.items())
        res.append("")  # add a new line at the end of the file
        return "\n".join(res)

    @staticmethod
    def grub_mkconfig() -> str:
        try:
            return subprocess_check_output("grub-mkconfig", raise_exception=True)
        except CalledProcessError as e:
            raise ValueError(
                f"grub-mkconfig failed: {e.returncode=}, {e.stderr=}, {e.stdout=}"
            ) from None

    @staticmethod
    def grub_reboot(idx: int):
        try:
            subprocess_call(f"grub-reboot {idx}", raise_exception=True)
        except CalledProcessError:
            logger.exception(f"failed to grub-reboot to {idx}")
            raise


class GrubABPartitionDetector:
    """A/B partition detector for ota-partition on grub booted system.

    Expected layout:
    (system boots with legacy BIOS)
        /dev/sdx
            - sdx1: dedicated boot partition
            - sdx2: A partition
            - sdx3: B partition

    or
    (system boots with UEFI)
        /dev/sdx
            - sdx1: /boot/uefi
            - sdx2: /boot
            - sdx3: A partition
            - sdx4: B partition

    We assume that last 2 partitions are A/B partitions, error will be raised
    if the current rootfs is not one of the last 2 partitions.

    NOTE(20231027): as workaround to rootfs not sda breaking OTA, slot naming schema
        is fixed to "sda<partition_id>", and ota-partition folder is searched with this name.
        For example, if current slot's device is nvme0n1p3, the slot_name is sda3.
    """

    # assuming that the suffix digit are the partiton id, for example,
    # sda3's pid is 3, nvme0n1p3's pid is also 3.
    DEV_PATH_PA: ClassVar[re.Pattern] = re.compile(
        r"^/dev/(?P<dev_name>\w*[a-z])(?P<partition_id>\d+)$"
    )
    SLOT_NAME_PREFIX: ClassVar[str] = "sda"

    def __init__(self) -> None:
        self.active_slot, self.active_dev = self._detect_active_slot()
        self.standby_slot, self.standby_dev = self._detect_standby_slot(self.active_dev)

    def _get_sibling_dev(self, active_dev: str) -> str:
        """
        NOTE: revert to use previous detection mechanism.
        TODO: refine this method.
        """
        parent = cmdhelper.get_parent_dev(active_dev)
        boot_dev = cmdhelper.get_dev_by_mount_point("/boot")
        if not boot_dev:
            _err_msg = "/boot is not mounted"
            logger.error(_err_msg)
            raise ValueError(_err_msg)

        # list children device file from parent device
        cmd = ["lsblk", "-Ppo", "NAME,FSTYPE", parent]
        try:
            cmd_result = subprocess_check_output(cmd, raise_exception=True)
        except Exception as e:
            _err_msg = f"failed to detect boot device family tree: {e!r}"
            logger.error(_err_msg)
            raise _GrubBootControllerError(_err_msg) from e

        # exclude parent dev
        output = cmd_result.splitlines()[1:]
        # FSTYPE="ext4" and
        # not (parent_device_file, root_device_file and boot_device_file)
        for blk in output:
            if (m := re.search(r'NAME="(.*)"\s+FSTYPE="(.*)"', blk)) and (
                m.group(1) != active_dev
                and m.group(1) != boot_dev
                and m.group(2) == "ext4"
            ):
                return m.group(1)

        _err_msg = f"{parent=} has unexpected partition layout: {output=}"
        logger.error(_err_msg)
        raise ValueError(_err_msg)

    def _detect_active_slot(self) -> Tuple[str, str]:
        """Get active slot's slot_id.

        Returns:
            A tuple contains the slot_name and the full dev path
            of the active slot.
        """
        try:
            dev_path = cmdhelper.get_current_rootfs_dev(cfg.ACTIVE_ROOT)
            assert dev_path
        except Exception as e:
            _err_msg = f"failed to detect current rootfs dev: {e!r}"
            logger.error(_err_msg)
            raise _GrubBootControllerError(_err_msg) from e

        _dev_path_ma = self.DEV_PATH_PA.match(dev_path)
        assert _dev_path_ma, f"dev path is invalid for OTA: {dev_path}"

        _pid = _dev_path_ma.group("partition_id")
        slot_name = f"{self.SLOT_NAME_PREFIX}{_pid}"
        return slot_name, dev_path

    def _detect_standby_slot(self, active_dev: str) -> Tuple[str, str]:
        """Get standby slot's slot_id.

        Returns:
            A tuple contains the slot_name and the full dev path
            of the standby slot.
        """
        dev_path = self._get_sibling_dev(active_dev)
        _dev_path_ma = self.DEV_PATH_PA.match(dev_path)
        assert _dev_path_ma, f"dev path is invalid for OTA: {dev_path}"

        _pid = _dev_path_ma.group("partition_id")
        slot_name = f"{self.SLOT_NAME_PREFIX}{_pid}"
        return slot_name, dev_path


class _GrubControl:
    """Implementation of ota-partition switch boot mechanism."""

    def __init__(self) -> None:
        ab_detector = GrubABPartitionDetector()
        self.active_root_dev = ab_detector.active_dev
        self.standby_root_dev = ab_detector.standby_dev
        self.active_slot = ab_detector.active_slot
        self.standby_slot = ab_detector.standby_slot
        logger.info(
            f"{self.active_slot=}@{self.active_root_dev}, {self.standby_slot=}@{self.standby_root_dev}"
        )

        self.boot_dir = Path(cfg.BOOT_DPATH)
        self.grub_file = Path(boot_cfg.GRUB_CFG_PATH)

        self.ota_partition_symlink = self.boot_dir / boot_cfg.BOOT_OTA_PARTITION_FILE
        self.active_ota_partition_folder = (
            self.boot_dir / boot_cfg.BOOT_OTA_PARTITION_FILE
        ).with_suffix(f".{self.active_slot}")
        self.active_ota_partition_folder.mkdir(exist_ok=True)

        self.standby_ota_partition_folder = (
            self.boot_dir / boot_cfg.BOOT_OTA_PARTITION_FILE
        ).with_suffix(f".{self.standby_slot}")
        self.standby_ota_partition_folder.mkdir(exist_ok=True)

        # NOTE: standby slot will be prepared in an OTA, GrubControl init will not check
        #       standby slot's ota-partition folder.
        self._grub_control_initialized = False
        self._check_active_slot_ota_partition_file()

    @property
    def initialized(self) -> bool:
        """Indicates whether grub_control migrates itself from non-OTA booted system,
        or recovered from a ota_partition files corrupted boot.

        Normally this property should be false, if it is true, OTAStatusControl should also
            initialize itself.
        """
        return self._grub_control_initialized

    def _check_active_slot_ota_partition_file(self):
        """Check and ensure active ota-partition files, init if needed.

        GrubControl supports migrates system that doesn't boot via ota-partition
            mechanism to using ota-partition mechanism. It also supports fixing ota-partition
            symlink missing or corrupted.

        NOTE:
        1. this method only update the ota-partition.<active_slot>/grub.cfg!
        2. standby slot is not considered here!
        3. expected booted kernel/initrd located under /boot.
        """
        # ------ check boot files ------ #
        vmlinuz_active_slot = self.active_ota_partition_folder / GrubHelper.KERNEL_OTA
        initrd_active_slot = self.active_ota_partition_folder / GrubHelper.INITRD_OTA
        active_slot_ota_boot_files_missing = (
            not vmlinuz_active_slot.is_file() or not initrd_active_slot.is_file()
        )

        try:
            kernel_booted_fpath, initrd_booted_fpath = self._get_current_booted_files()
            kernel_booted, initrd_booted = (
                Path(kernel_booted_fpath).name,
                Path(initrd_booted_fpath).name,
            )

            # NOTE: current slot might be booted with ota(normal), or ota.standby(during update)
            not_booted_with_ota_mechanism = kernel_booted not in (
                GrubHelper.KERNEL_OTA,
                GrubHelper.KERNEL_OTA_STANDBY,
            ) or initrd_booted not in (
                GrubHelper.INITRD_OTA,
                GrubHelper.INITRD_OTA_STANDBY,
            )
        except Exception as e:
            logger.error(
                f"failed to get current booted kernel and initrd.image: {e!r}, "
                "try to use active slot ota-partition files"
            )
            kernel_booted, initrd_booted = vmlinuz_active_slot, initrd_active_slot
            not_booted_with_ota_mechanism = True

        ota_partition_symlink_missing = not self.ota_partition_symlink.is_symlink()

        if (
            not_booted_with_ota_mechanism
            or active_slot_ota_boot_files_missing
            or ota_partition_symlink_missing
        ):
            logger.warning(
                "system is not booted with ota mechanism("
                f"{not_booted_with_ota_mechanism=}, {active_slot_ota_boot_files_missing=}, {ota_partition_symlink_missing=}), "
                f"migrating and initializing ota-partition files for {self.active_slot}@{self.active_root_dev}..."
            )

            # NOTE: just copy but not cleanup the booted kernel/initrd files
            if active_slot_ota_boot_files_missing:
                shutil.copy(
                    self.boot_dir / kernel_booted,
                    self.active_ota_partition_folder,
                    follow_symlinks=True,
                )
                shutil.copy(
                    self.boot_dir / initrd_booted,
                    self.active_ota_partition_folder,
                    follow_symlinks=True,
                )

            # recreate all ota-partition files for active slot
            self._prepare_kernel_initrd_links(self.active_ota_partition_folder)
            self._ensure_ota_partition_symlinks(active_slot=self.active_slot)
            self._ensure_standby_slot_boot_files_symlinks(
                standby_slot=self.standby_slot
            )
            self._grub_update_on_booted_slot()
            self._grub_control_initialized = True

        logger.info(f"ota-partition files for {self.active_slot} are ready")

    @staticmethod
    def _get_current_booted_files() -> Tuple[str, str]:
        """Return the name of booted kernel and initrd.

        Expected booted kernel and initrd are located under /boot.
        """
        boot_cmdline = cat_proc_cmdline()
        if kernel_ma := re.search(
            r"BOOT_IMAGE=.*(?P<kernel>vmlinuz-(?P<ver>[\w\.\-]*))",
            boot_cmdline,
        ):
            kernel_ver = kernel_ma.group("ver")
        else:
            raise ValueError("failed to detect booted linux kernel")

        # lookup the grub file and find the booted entry
        # NOTE(20230905): use standard way to find initrd img
        initrd_img = f"{GrubHelper.INITRD}{GrubHelper.FNAME_VER_SPLITTER}{kernel_ver}"
        if not (Path(cfg.BOOT_DPATH) / initrd_img).is_file():
            raise ValueError(f"failed to find booted initrd image({initrd_img})")
        return kernel_ma.group("kernel"), initrd_img

    @staticmethod
    def _prepare_kernel_initrd_links(target_folder: Path):
        """Prepare OTA symlinks for kernel/initrd under specific ota-partition folder.
        vmlinuz-ota -> vmlinuz-*
        initrd-ota -> initrd-*
        """
        kernel, initrd = None, None
        # NOTE(20230914): if multiple kernels presented, the first found
        #                 kernel(along with corresponding initrd.img) will be used.
        for f in target_folder.glob("*"):
            if (
                not f.is_symlink()
                and (_kernel_fname := f.name) != GrubHelper.KERNEL_OTA
                and (kernel_ma := GrubHelper.kernel_fname_pa.match(f.name))
            ):
                kernel_ver = kernel_ma.group("ver")
                _initrd_fname = (
                    f"{GrubHelper.INITRD}{GrubHelper.FNAME_VER_SPLITTER}{kernel_ver}"
                )
                if (target_folder / _initrd_fname).is_file():
                    kernel, initrd = _kernel_fname, _initrd_fname
                    break
        if not (kernel and initrd):
            raise ValueError(f"vmlinuz and/or initrd.img not found at {target_folder}")

        symlink_atomic(target_folder / GrubHelper.KERNEL_OTA, kernel)
        symlink_atomic(target_folder / GrubHelper.INITRD_OTA, initrd)
        logger.info(f"finished generate ota symlinks under {target_folder}")

    def _grub_update_on_booted_slot(self, *, abort_on_standby_missed=True):
        """Update grub_default and generate grub.cfg for current booted slot.

        NOTE:
        1. this method only ensures the entry existence for current booted slot.
        2. this method ensures the default entry to be the current booted slot.
        """
        grub_default_file = Path(cfg.ACTIVE_ROOT) / Path(
            boot_cfg.DEFAULT_GRUB_PATH
        ).relative_to(cfg.CANONICAL_ROOT)

        # NOTE: If the path points to a symlink, exists() returns
        #       whether the symlink points to an existing file or directory.
        active_vmlinuz = self.boot_dir / GrubHelper.KERNEL_OTA
        active_initrd = self.boot_dir / GrubHelper.INITRD_OTA
        if not (active_vmlinuz.is_file() and active_initrd.is_file()):
            msg = (
                "/boot/vmlinuz-ota and/or /boot/initrd.img-ota are broken, "
                "refuse to do grub-update"
            )
            logger.error(msg)
            raise _GrubBootControllerError(msg)

        # step1: update grub_default file
        _in = grub_default_file.read_text()
        _out = GrubHelper.update_grub_default(_in)
        write_str_to_file_atomic(grub_default_file, _out)

        # step2: generate grub_cfg by grub-mkconfig
        # parse the output and find the active slot boot entry idx
        grub_cfg_content = GrubHelper.grub_mkconfig()
        if res := GrubHelper.get_entry(
            grub_cfg_content, kernel_ver=GrubHelper.SUFFIX_OTA
        ):
            active_slot_entry_idx, _ = res
        else:
            _err_msg = "boot entry for ACTIVE slot not found, abort"
            logger.error(_err_msg)
            raise _GrubBootControllerError(_err_msg)

        # step3: update grub_default again, setting default to <idx>
        # ensure the active slot to be the default
        logger.info(
            f"boot entry for vmlinuz-ota(slot={self.active_slot}): {active_slot_entry_idx}"
        )
        _out = GrubHelper.update_grub_default(
            grub_default_file.read_text(),
            default_entry_idx=active_slot_entry_idx,
        )
        logger.debug(f"generated grub_default: {pformat(_out)}")
        write_str_to_file_atomic(grub_default_file, _out)

        # step4: populate new active grub_file
        # update the ota.standby entry's rootfs uuid to standby slot's uuid
        active_slot_grub_file = (
            self.active_ota_partition_folder / boot_cfg.GRUB_CFG_FNAME
        )

        grub_cfg_content = GrubHelper.grub_mkconfig()
        try:
            standby_uuid = cmdhelper.get_attrs_by_dev("UUID", self.standby_root_dev)
            assert standby_uuid
        except Exception as e:
            _err_msg = f"failed to get UUID of {self.standby_root_dev}: {e!r}"
            logger.error(_err_msg)
            raise _GrubBootControllerError(_err_msg) from e

        standby_uuid_str = f"UUID={standby_uuid}"
        if grub_cfg_updated := GrubHelper.update_entry_rootfs(
            grub_cfg_content,
            kernel_ver=GrubHelper.SUFFIX_OTA_STANDBY,
            rootfs_str=f"root={standby_uuid_str}",
        ):
            write_str_to_file_atomic(active_slot_grub_file, grub_cfg_updated)
            logger.info(f"standby rootfs: {standby_uuid_str}")
            logger.debug(f"generated grub_cfg: {pformat(grub_cfg_updated)}")
        else:
            msg = (
                "/boot/vmlinuz-ota.standby and/or /boot/initrd.img-ota.standby not found, "
                "only current active slot's entry is populated."
            )
            if abort_on_standby_missed:
                logger.error(msg)
                raise _GrubBootControllerError(msg)

            logger.warning(msg)
            logger.info(f"generated grub_cfg: {pformat(grub_cfg_content)}")
            write_str_to_file_atomic(active_slot_grub_file, grub_cfg_content)

        # finally, point grub.cfg to active slot's grub.cfg
        symlink_atomic(  # /boot/grub/grub.cfg -> ../ota-partition/grub.cfg
            self.grub_file,
            Path("../") / boot_cfg.BOOT_OTA_PARTITION_FILE / "grub.cfg",
        )
        logger.info(f"update_grub for {self.active_slot} finished.")

    def _ensure_ota_partition_symlinks(self, active_slot: str):
        """Ensure /boot/{ota_partition,vmlinuz-ota,initrd.img-ota} symlinks from
        specified <active_slot> point's of view."""
        ota_partition_folder = Path(boot_cfg.BOOT_OTA_PARTITION_FILE)  # ota-partition
        symlink_atomic(  # /boot/ota-partition -> ota-partition.<active_slot>
            self.boot_dir / ota_partition_folder,
            ota_partition_folder.with_suffix(f".{active_slot}"),
        )
        symlink_atomic(  # /boot/vmlinuz-ota -> ota-partition/vmlinuz-ota
            self.boot_dir / GrubHelper.KERNEL_OTA,
            ota_partition_folder / GrubHelper.KERNEL_OTA,
        )
        symlink_atomic(  # /boot/initrd.img-ota -> ota-partition/initrd.img-ota
            self.boot_dir / GrubHelper.INITRD_OTA,
            ota_partition_folder / GrubHelper.INITRD_OTA,
        )

    def _ensure_standby_slot_boot_files_symlinks(self, standby_slot: str):
        """Ensure boot files symlinks for specified <standby_slot>."""
        ota_partition_folder = Path(boot_cfg.BOOT_OTA_PARTITION_FILE)  # ota-partition
        symlink_atomic(  # /boot/vmlinuz-ota.standby -> ota-partition.<standby_slot>/vmlinuz-ota
            self.boot_dir / GrubHelper.KERNEL_OTA_STANDBY,
            ota_partition_folder.with_suffix(f".{standby_slot}")
            / GrubHelper.KERNEL_OTA,
        )
        symlink_atomic(  # /boot/initrd.img-ota.standby -> ota-partition.<standby_slot>/initrd.img-ota
            self.boot_dir / GrubHelper.INITRD_OTA_STANDBY,
            ota_partition_folder.with_suffix(f".{standby_slot}")
            / GrubHelper.INITRD_OTA,
        )

    # API

    def finalize_update_switch_boot(self):
        """Finalize switch boot and use boot files from current booted slot."""
        # NOTE: since we have not yet switched boot, the active/standby relationship is
        #       reversed here corresponding to booted slot.
        try:
            self._prepare_kernel_initrd_links(self.standby_ota_partition_folder)
            self._ensure_ota_partition_symlinks(active_slot=self.standby_slot)
            self._ensure_standby_slot_boot_files_symlinks(standby_slot=self.active_slot)

            self._grub_update_on_booted_slot(abort_on_standby_missed=True)

            # switch ota-partition symlink to current booted slot
            self._ensure_ota_partition_symlinks(active_slot=self.active_slot)
            self._ensure_standby_slot_boot_files_symlinks(
                standby_slot=self.standby_slot
            )
            return True
        except Exception as e:
            _err_msg = f"grub: failed to finalize switch boot: {e!r}"
            logger.error(_err_msg)
            raise _GrubBootControllerError(_err_msg) from e

    def grub_reboot_to_standby(self):
        """Temporarily boot to standby slot after OTA applied to standby slot."""
        # ensure all required symlinks for standby slot are presented and valid
        try:
            self._prepare_kernel_initrd_links(self.standby_ota_partition_folder)
            self._ensure_standby_slot_boot_files_symlinks(
                standby_slot=self.standby_slot
            )

            # ensure all required symlinks for active slot are presented and valid
            # NOTE: reboot after post-update is still using the current active slot's
            #       ota-partition symlinks(not yet switch boot).
            self._prepare_kernel_initrd_links(self.active_ota_partition_folder)
            self._ensure_ota_partition_symlinks(active_slot=self.active_slot)
            self._grub_update_on_booted_slot(abort_on_standby_missed=True)

            idx, _ = GrubHelper.get_entry(
                read_str_from_file(self.grub_file),
                kernel_ver=GrubHelper.SUFFIX_OTA_STANDBY,
            )
            GrubHelper.grub_reboot(idx)
            logger.info(f"system will reboot to {self.standby_slot=}: boot entry {idx}")

        except Exception as e:
            _err_msg = f"grub: failed to grub_reboot to standby: {e!r}"
            logger.error(_err_msg)
            raise _GrubBootControllerError(_err_msg) from e


class GrubController(BootControllerProtocol):
    def __init__(self) -> None:
        try:
            self._boot_control = _GrubControl()
            self._mp_control = SlotMountHelper(
                standby_slot_dev=self._boot_control.standby_root_dev,
                standby_slot_mount_point=cfg.STANDBY_SLOT_MNT,
                active_rootfs=cfg.ACTIVE_ROOT,
                active_slot_mount_point=cfg.ACTIVE_SLOT_MNT,
            )
            self._ota_status_control = OTAStatusFilesControl(
                active_slot=self._boot_control.active_slot,
                standby_slot=self._boot_control.standby_slot,
                current_ota_status_dir=self._boot_control.active_ota_partition_folder,
                standby_ota_status_dir=self._boot_control.standby_ota_partition_folder,
                finalize_switching_boot=self._boot_control.finalize_update_switch_boot,
                # NOTE(20230904): if boot control is initialized(i.e., migrate from non-ota booted system),
                #                 force initialize the ota_status files.
                force_initialize=self._boot_control.initialized,
            )
        except Exception as e:
            _err_msg = f"failed on start grub boot controller: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlStartupFailed(_err_msg, module=__name__) from e

    def _update_fstab(self, *, active_slot_fstab: Path, standby_slot_fstab: Path):
        """Update standby fstab based on active slot's fstab and just installed new stanby fstab.

        Override existed entries in standby fstab, merge new entries from active fstab.
        """
        try:
            standby_uuid = cmdhelper.get_attrs_by_dev(
                "UUID", self._boot_control.standby_root_dev
            )
            assert standby_uuid
        except Exception as e:
            _err_msg = (
                f"failed to get UUID of {self._boot_control.standby_root_dev}: {e!r}"
            )
            logger.error(_err_msg)
            raise _GrubBootControllerError(_err_msg) from e

        standby_uuid_str = f"UUID={standby_uuid}"
        fstab_entry_pa = re.compile(
            r"^\s*(?P<file_system>[^# ]*)\s+"
            r"(?P<mount_point>[^ ]*)\s+"
            r"(?P<type>[^ ]*)\s+"
            r"(?P<options>[^ ]*)\s+"
            r"(?P<dump>[\d]*)\s+(?P<pass>[\d]*)",
            re.MULTILINE,
        )

        # standby partition fstab (to be merged)
        fstab_standby = read_str_from_file(standby_slot_fstab)
        fstab_standby_dict: Dict[str, re.Match] = {}

        for line in fstab_standby.splitlines():
            ma = fstab_entry_pa.match(line)
            if ma and ma.group("mount_point") != "/":
                fstab_standby_dict[ma.group("mount_point")] = ma

        # merge entries
        merged: List[str] = []
        fstab_active = read_str_from_file(active_slot_fstab)
        for line in fstab_active.splitlines():
            if ma := fstab_entry_pa.match(line):
                mp = ma.group("mount_point")
                if mp == "/":  # rootfs mp, unconditionally replace uuid
                    _list = list(ma.groups())
                    _list[0] = standby_uuid_str
                    merged.append("\t".join(_list))
                elif mp in fstab_standby_dict:
                    merged.append("\t".join(fstab_standby_dict[mp].groups()))
                    del fstab_standby_dict[mp]
                else:
                    merged.append("\t".join(ma.groups()))
            elif line.strip().startswith("#"):
                merged.append(line)  # re-add comments to merged

        # merge standby_fstab's left-over lines
        for _, ma in fstab_standby_dict.items():
            merged.append("\t".join(ma.groups()))
        merged.append("")  # add a new line at the end of file

        # write to standby fstab
        write_str_to_file_atomic(standby_slot_fstab, "\n".join(merged))

    def _cleanup_standby_ota_partition_folder(self):
        """Cleanup old files under the standby ota-partition folder."""
        files_kept = (
            cfg.OTA_STATUS_FNAME,
            cfg.OTA_VERSION_FNAME,
            cfg.SLOT_IN_USE_FNAME,
            boot_cfg.GRUB_CFG_FNAME,
        )
        removes = (
            f
            for f in self._ota_status_control.standby_ota_status_dir.glob("*")
            if f.name not in files_kept
        )
        for f in removes:
            if f.is_dir():
                shutil.rmtree(f, ignore_errors=True)
            else:
                f.unlink(missing_ok=True)

    def _copy_boot_files_from_standby_slot(self):
        """Copy boot files under <standby_slot_mp>/boot to standby ota-partition folder."""
        standby_ota_partition_dir = self._ota_status_control.standby_ota_status_dir
        for f in self._mp_control.standby_boot_dir.iterdir():
            if f.is_file() and not f.is_symlink():
                shutil.copy(f, standby_ota_partition_dir)

    # API

    def get_standby_slot_path(self) -> Path:  # pragma: no cover
        return self._mp_control.standby_slot_mount_point

    def get_standby_boot_dir(self) -> Path:  # pragma: no cover
        return self._mp_control.standby_boot_dir

    def load_version(self) -> str:  # pragma: no cover
        return self._ota_status_control.load_active_slot_version()

    def get_booted_ota_status(self) -> OTAStatus:  # pragma: no cover
        return self._ota_status_control.booted_ota_status

    def on_operation_failure(self):
        """Failure registering and cleanup at failure."""
        logger.warning("on failure try to unmounting standby slot...")
        self._ota_status_control.on_failure()
        self._mp_control.umount_all(ignore_error=True)

    def pre_update(self, version: str, *, standby_as_ref: bool, erase_standby=False):
        try:
            logger.info("grub_boot: pre-update setup...")
            ### udpate active slot's ota_status ###
            self._ota_status_control.pre_update_current()

            ### mount slots ###
            self._mp_control.prepare_standby_dev(erase_standby=erase_standby)
            self._mp_control.mount_standby()
            self._mp_control.mount_active()

            ### update standby slot's ota_status files ###
            self._ota_status_control.pre_update_standby(version=version)

            # remove old files under standby ota_partition folder
            self._cleanup_standby_ota_partition_folder()
        except Exception as e:
            _err_msg = f"failed on pre_update: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPreUpdateFailed(
                _err_msg, module=__name__
            ) from e

    def post_update(self) -> None:
        try:
            logger.info("grub_boot: post-update setup...")
            # ------ update fstab ------ #
            active_fstab = self._mp_control.active_slot_mount_point / Path(
                boot_cfg.FSTAB_FILE_PATH
            ).relative_to("/")
            standby_fstab = self._mp_control.standby_slot_mount_point / Path(
                boot_cfg.FSTAB_FILE_PATH
            ).relative_to("/")
            self._update_fstab(
                standby_slot_fstab=standby_fstab,
                active_slot_fstab=active_fstab,
            )

            # ------ prepare boot files ------ #
            self._copy_boot_files_from_standby_slot()

            # ------ pre-reboot ------ #
            self._mp_control.umount_all(ignore_error=True)
            self._boot_control.grub_reboot_to_standby()
        except Exception as e:
            _err_msg = f"failed on post_update: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPostUpdateFailed(
                _err_msg, module=__name__
            ) from e

    def finalizing_update(self) -> NoReturn:
        try:
            cmdhelper.reboot()
        except Exception as e:
            _err_msg = f"reboot failed: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPostUpdateFailed(
                _err_msg, module=__name__
            ) from e

    def pre_rollback(self):
        try:
            logger.info("grub_boot: pre-rollback setup...")
            self._ota_status_control.pre_rollback_current()
            self._mp_control.mount_standby()
            self._ota_status_control.pre_rollback_standby()
        except Exception as e:
            _err_msg = f"failed on pre_rollback: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPreRollbackFailed(
                _err_msg, module=__name__
            ) from e

    def post_rollback(self):
        try:
            logger.info("grub_boot: post-rollback setup...")
            self._boot_control.grub_reboot_to_standby()
            self._mp_control.umount_all(ignore_error=True)
        except Exception as e:
            _err_msg = f"failed on pre_rollback: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPostRollbackFailed(
                _err_msg, module=__name__
            ) from e

    finalizing_rollback = finalizing_update
