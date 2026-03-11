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

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from subprocess import CalledProcessError
from typing import ClassVar, Generator, Literal, NoReturn, Optional

from typing_extensions import Self

from _otaclient_version import version
from otaclient import errors as ota_errors
from otaclient.boot_control._base import BootControllerBase
from otaclient.boot_control._ota_status_control import OTAStatusFilesControl
from otaclient.boot_control._slot_mnt_helper import SlotMountHelper
from otaclient.configs.cfg import cfg
from otaclient_common import _env, cmdhelper, replace_root
from otaclient_common._io import (
    copyfile_atomic,
    read_str_from_file,
    remove_file,
    write_str_to_file_atomic,
)
from otaclient_common._typing import StrEnum
from otaclient_common.linux import subprocess_run_wrapper

from .configs import grub_new_cfg as boot_cfg

logger = logging.getLogger(__name__)

VMLINUZ_PREFIX = "vmlinuz-"
INITRD_PREFIX = "initrd.img-"

GRUB_DEFAULT_OPTIONS = {
    "GRUB_TIMEOUT_STYLE": "menu",
    "GRUB_TIMEOUT": "0",
    "GRUB_DISABLE_SUBMENU": "y",
    "GRUB_DISABLE_OS_PROBER": "true",
    "GRUB_DISABLE_RECOVERY": "true",
    "GRUB_DEFAULT": "saved",
}
"""The required grub options for OTA grub boot control."""
GRUB_BLACKLIST_OPTIONS = ["GRUB_SAVEDEFAULT"]
"""The grub options that MUST be stripped away."""


class GrubBootControllerError(Exception):
    """Grub boot controller internal used exception."""


OTA_MANAGED_CFG_HEADER = (
    "# OTAClient managed configuration file, DO NOT EDIT!\n"
    "# Manual edits to this file will NOT be preserved across OTA!\n"
)


@dataclass
class OTAManagedCfg:
    """Represents an OTA managed configuration file.

    OTA managed boot cfg files (e.g. grub.cfg) are generated and maintained by
    otaclient. Each file includes a header identifying it as OTA managed, and a
    footer containing metadata (otaclient version, grub version, and a checksum).

    Use `validate_managed_config` to check whether an existing config file is a
    valid OTA managed config. If validation passes, the config does not need to
    be regenerated. Use `export` to render the instance back into a file string.

    The file layout is:

    ```
    # OTAClient managed configuration file, DO NOT EDIT!
    # Manual edits to this file will NOT be preserved across OTA!
    <raw_contents>
    # ------ OTA managed metadata ------ #
    # otaclient_version: vx.xx.x
    # grub_version: x.xx
    # checksum: sha256:xxxxxxx
    # ------ End of OTA managed metadata ------ #
    ```
    """

    HEADER: ClassVar[str] = OTA_MANAGED_CFG_HEADER
    FOOTER_HEAD: ClassVar[str] = "# ------ OTA managed metadata ------ #\n"
    FOOTER_TAIL: ClassVar[str] = "# ------ End of OTA managed metadata ------ #\n"

    raw_contents: str
    grub_version: str
    checksum: str
    otaclient_version: str = version

    def __post_init__(self) -> None:
        self.raw_contents = self.raw_contents.strip()

    @staticmethod
    def _parse_footer(_footer: str) -> dict[str, str]:
        _res: dict[str, str] = {}
        for _line in _footer.splitlines():
            if _line.startswith("# ------"):
                continue
            _line = _line.strip()
            if not _line.startswith("#"):
                raise ValueError

            _line = _line.lstrip("#").strip()
            _k, _, _v = _line.partition(":")
            _res[_k.strip()] = _v.strip()
        return _res

    @classmethod
    def validate_managed_config(cls, _in: str) -> Self | None:
        if not _in.startswith(cls.HEADER):
            return
        _in = _in[len(cls.HEADER) :]

        _footer_start = _in.find(cls.FOOTER_HEAD)
        _footer_end = _in.find(cls.FOOTER_TAIL)
        if _footer_start < 0 or _footer_end < 0:
            return

        try:
            metadata = cls._parse_footer(
                _in[_footer_start : _footer_end + len(cls.FOOTER_TAIL)]
            )
        except ValueError:
            return

        checksum = metadata.get("checksum")
        grub_version = metadata.get("grub_version")
        otaclient_version = metadata.get("otaclient_version")
        if not (checksum and grub_version and otaclient_version):
            return

        content = _in[:_footer_start].strip()
        _algorithm, _expected_digest = checksum.split(":", 1)
        try:
            _hasher = hashlib.new(_algorithm, content.encode())
        except ValueError:
            return

        if _hasher.hexdigest() != _expected_digest:
            return

        return cls(
            raw_contents=content,
            grub_version=grub_version,
            checksum=checksum,
            otaclient_version=otaclient_version,
        )

    def export(self, *, hash_algorithm: str = "sha256") -> str:
        _content = self.raw_contents
        _digest = hashlib.new(hash_algorithm, _content.encode()).hexdigest()
        _footer = (
            f"{self.FOOTER_HEAD}"
            f"# otaclient_version: {self.otaclient_version}\n"
            f"# grub_version: {self.grub_version}\n"
            f"# checksum: {hash_algorithm}:{_digest}\n"
            f"{self.FOOTER_TAIL}"
        )
        return f"{self.HEADER}{_content}{_footer}"


class OTASlotBootID(StrEnum):
    slot_a = f"{boot_cfg.OTA_BOOT_SLOT_BASE}{boot_cfg.SLOT_A_SUFFIX}"
    slot_b = f"{boot_cfg.OTA_BOOT_SLOT_BASE}{boot_cfg.SLOT_B_SUFFIX}"

    def get_suffix(self) -> str:
        return f"_{self.rsplit('_', 1)[-1]}"


MENUENTRY_HEAD_PA = re.compile(r"^\s*menuentry\s", re.MULTILINE)
LINUX_PA_MULTILINE = re.compile(r"^\s*linux\s*(?P<linux_fpath>[^\s]+)", re.MULTILINE)
LINUX_VERSION_PA = re.compile(r"vmlinuz-(?P<ver>[\.\w-]+)$")
INITRD_PA_MULTILINE = re.compile(r"^\s*initrd\s*(?P<initrd_fpath>[^\s]+)", re.MULTILINE)
MENUENTRY_TITLE_PA = re.compile(
    r"""^\s*menuentry\s+(?P<entry_title>(?:"[^"]*"|'[^']*'|[^\s]+))"""
)
MENUENTRY_ID_PA = re.compile(
    r"""^\s*menuentry.*?\$menuentry_id_option\s+(?P<entry_id>(?:"[^"]*"|'[^']*'|[^\s]+))"""
)


def _iter_menuentries(_in: str) -> Generator[str]:
    """Extract all menuentry blocks from grub-mkconfig output.

    Uses brace-depth counting to correctly handle nested {} blocks
    (e.g. if/fi statements with braces) within menuentry bodies.

    Raises:
        ValueError: If a menuentry has no opening brace or unclosed braces.
    """
    for ma in MENUENTRY_HEAD_PA.finditer(_in):
        # find the opening brace after "menuentry ..."
        _brace_start = _in.find("{", ma.start())
        if _brace_start < 0:
            raise ValueError(f"menuentry at offset {ma.start()} has no opening brace")

        _depth = 0
        for i in range(_brace_start, len(_in)):
            if _in[i] == "{":
                _depth += 1
            elif _in[i] == "}":
                _depth -= 1
                if _depth == 0:
                    yield _in[ma.start() : i + 1]
                    break
        else:
            raise ValueError(f"menuentry at offset {ma.start()} has unclosed braces")


@dataclass
class _BootMenuEntry:
    raw_entry: str
    slot_boot_id: OTASlotBootID
    kernel_ver: str

    @classmethod
    def _find_menuentry(cls, _in: str, *, kernel_ver: str) -> str:
        """Find a raw menuentry block from the input with matching kernel version.

        Iterates all menuentry blocks in the grub-mkconfig output, skipping
        recovery entries and entries without a valid linux directive, and
        returns the first entry whose kernel version matches.

        Args:
            _in (str): The full grub-mkconfig output string.
            kernel_ver (str): The kernel version to match (e.g. "5.19.0-50-generic").

        Returns:
            str: The raw menuentry block string.

        Raises:
            ValueError: If no matching non-recovery menuentry is found.
        """
        for _found in _iter_menuentries(_in):
            _linux_dir_ma = LINUX_PA_MULTILINE.search(_found)
            if not _linux_dir_ma:
                continue  # not a linux boot menuentry

            _linux_dir = _linux_dir_ma.group()
            if _linux_dir.find("recovery") >= 0:
                continue  # skip recovery entry

            _linux_ver_ma = LINUX_VERSION_PA.search(_linux_dir)
            if not _linux_ver_ma:
                continue  # invalid linux entry

            if _linux_ver_ma.group("ver") != kernel_ver:
                continue  # not the entry we are looking for
            return _found

        raise ValueError(f"failed to find menuentry for kernel version {kernel_ver!r}")

    @classmethod
    def _fixup_menuentry(cls, _entry: str, *, slot_boot_id: OTASlotBootID) -> str:
        """Fix up a raw menuentry block for OTA slot boot.

        Performs the following rewrites on the menuentry block:
            1. Replaces the menuentry title and id with `slot_boot_id`.
            2. Prefixes the linux and initrd file paths with `/<slot_boot_id>/`.

        Args:
            _entry (str): The raw menuentry block string to fix up.
            slot_boot_id (OTASlotBootID): The OTA slot boot identifier to apply.

        Returns:
            str: The rewritten menuentry block string.
        """
        _entry = MENUENTRY_TITLE_PA.sub(
            lambda ma: ma.group().replace(ma.group("entry_title"), slot_boot_id, 1),
            _entry,
        )
        _entry = MENUENTRY_ID_PA.sub(
            lambda ma: ma.group().replace(ma.group("entry_id"), slot_boot_id, 1),
            _entry,
        )

        _entry = LINUX_PA_MULTILINE.sub(
            lambda ma: ma.group().replace(
                ma.group("linux_fpath"), f"/{slot_boot_id}{ma.group('linux_fpath')}", 1
            ),
            _entry,
        )
        _entry = INITRD_PA_MULTILINE.sub(
            lambda ma: ma.group().replace(
                ma.group("initrd_fpath"),
                f"/{slot_boot_id}{ma.group('initrd_fpath')}",
                1,
            ),
            _entry,
        )
        return _entry

    @classmethod
    def generate_menuentry(
        cls, _in: str, *, slot_boot_id: OTASlotBootID, kernel_ver: str
    ) -> Self:
        """Generate an OTA-ready menuentry from grub-mkconfig output.

        Finds the menuentry block matching `kernel_ver`, then rewrites its
        title, id, and file paths for the given `slot_boot_id`.

        Args:
            _in (str): The full grub-mkconfig output string.
            slot_boot_id (OTASlotBootID): The OTA slot boot identifier to apply.
            kernel_ver (str): The kernel version to match (e.g. "5.19.0-50-generic").

        Returns:
            _BootMenuEntry: A `_BootMenuEntry` instance with the fixed-up menuentry.

        Raises:
            ValueError: If no matching non-recovery menuentry is found.
        """
        # NOTE: for specific kernel version, we should have exactly one
        #       boot entry(non-recovery entry) for it.
        return cls(
            raw_entry=cls._fixup_menuentry(
                cls._find_menuentry(_in, kernel_ver=kernel_ver),
                slot_boot_id=slot_boot_id,
            ),
            slot_boot_id=slot_boot_id,
            kernel_ver=kernel_ver,
        )


@dataclass
class _SlotInfo:
    dev: str
    uuid: str
    slot_id: Optional[OTASlotBootID] = None

    @classmethod
    def from_partinfo(
        cls, _partinfo: _PartitionInfo, _slot_id: OTASlotBootID | None = None
    ) -> Self:
        return cls(slot_id=_slot_id, dev=_partinfo.dev, uuid=_partinfo.uuid)


@dataclass
class _ABPartition:
    is_uefi: bool

    boot_partition: _SlotInfo
    slot_a: _SlotInfo
    slot_b: _SlotInfo
    current_slot: OTASlotBootID
    standby_slot: OTASlotBootID
    efi_partition: Optional[_SlotInfo] = None


DEV_PATH_PA = re.compile(r"^/dev/(?P<dev_name>\w*[a-z])(?P<partition_id>\d+)$")
EFI_PARTTYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"


@dataclass
class _PartitionInfo:
    dev: str
    uuid: str
    parttype: str


class ABPartitionDetector:
    """Detected A/B partition layout for OTA grub boot control.

    Supported partition layouts:

        UEFI (first partition is vfat):
            /dev/<disk>
                - p1: EFI system partition (vfat)
                - p2: /boot (ext4)
                - p3: ota-slot_a
                - p4: ota-slot_b

        Legacy BIOS (first partition is ext4):
            /dev/<disk>
                - p1: /boot (ext4)
                - p2: ota-slot_a
                - p3: ota-slot_b
    """

    @staticmethod
    def _detect_rootfs_dev() -> tuple[str, str]:
        try:
            _dev_path = cmdhelper.get_current_rootfs_dev(
                active_root=cfg.CANONICAL_ROOT,
                chroot=_env.get_dynamic_client_chroot_path(),
            )
            assert _dev_path
        except Exception as e:
            _err_msg = f"failed to detect current rootfs dev: {e!r}"
            logger.error(_err_msg)
            raise GrubBootControllerError(_err_msg) from e

        if not (_ma := DEV_PATH_PA.match(_dev_path)):
            raise GrubBootControllerError(f"unexpected rootfs dev: {_dev_path}")
        return _dev_path, _ma.group("partition_id")

    @staticmethod
    def _list_partitions(rootfs_dev: str) -> dict[str, _PartitionInfo]:
        _parts: dict[str, _PartitionInfo] = {}
        _lsblk_pa = re.compile(
            r'NAME="(?P<dev_name>[^"]+)"\s+FSTYPE="(?P<fstype>[^"]*)"\s+UUID="(?P<uuid>[^"]*)"\s+PARTTYPE="(?P<parttype>[^"]*)"'
        )
        _lsblk_cmd = ["lsblk", "-Ppo", "NAME,FSTYPE,UUID,PARTTYPE"]
        try:
            _parent_dev = cmdhelper.get_parent_dev(rootfs_dev)
            _output = cmdhelper.subprocess_check_output(
                [*_lsblk_cmd, _parent_dev], raise_exception=True
            )

            # skip the first line (parent device itself)
            for _entry in _output.splitlines()[1:]:
                if not (_entry_ma := _lsblk_pa.search(_entry)):
                    continue
                _dev_name = _entry_ma.group("dev_name")

                _dev_path_ma = DEV_PATH_PA.search(_dev_name)
                assert _dev_path_ma
                _part_id = _dev_path_ma.group("partition_id")

                _parts[_part_id] = _PartitionInfo(
                    dev=_dev_name,
                    uuid=_entry_ma.group("uuid"),
                    parttype=_entry_ma.group("parttype"),
                )
            return _parts
        except Exception as e:
            _err_msg = f"failed to detect boot device family tree: {e!r}"
            logger.error(_err_msg)
            raise GrubBootControllerError(_err_msg) from e

    @classmethod
    def detect_boot_slots(cls) -> _ABPartition:
        _rootfs_dev, _active_partid = cls._detect_rootfs_dev()
        _partitions = cls._list_partitions(_rootfs_dev)

        if "1" not in _partitions:
            raise GrubBootControllerError(f"missing first partition: {_partitions=}")
        if _partitions["1"].parttype.lower() == EFI_PARTTYPE:
            if not all(str(_pid) in _partitions for _pid in range(1, 5)):
                raise GrubBootControllerError(
                    f"unexpected layout for UEFI booted system: {_partitions=}"
                )

            if _active_partid not in ["3", "4"]:
                raise GrubBootControllerError(
                    f"booted from unexpected partition: {_active_partid=}"
                )

            # fmt: off
            return _ABPartition(
                is_uefi=True,
                efi_partition=_SlotInfo.from_partinfo(_partitions["1"]),
                boot_partition=_SlotInfo.from_partinfo(_partitions["2"]),
                slot_a=_SlotInfo.from_partinfo(_partitions["3"], OTASlotBootID.slot_a),
                slot_b=_SlotInfo.from_partinfo(_partitions["4"], OTASlotBootID.slot_b),
                current_slot=OTASlotBootID.slot_a if _active_partid == "3" else OTASlotBootID.slot_b,
                standby_slot=OTASlotBootID.slot_a if _active_partid == "4" else OTASlotBootID.slot_b,
            )
            # fmt: on
        else:  # legacy BIOS system
            if not all(str(_pid) in _partitions for _pid in range(1, 4)):
                raise GrubBootControllerError(
                    f"unexpected layout for legacy BIOS booted system: {_partitions}"
                )

            if _active_partid not in ["2", "3"]:
                raise GrubBootControllerError(
                    f"booted from unexpected partition: {_active_partid=}"
                )

            # fmt: off
            return _ABPartition(
                is_uefi=False,
                boot_partition=_SlotInfo.from_partinfo(_partitions["1"]),
                slot_a=_SlotInfo.from_partinfo(_partitions["2"], OTASlotBootID.slot_a),
                slot_b=_SlotInfo.from_partinfo(_partitions["3"], OTASlotBootID.slot_b),
                current_slot=OTASlotBootID.slot_a if _active_partid == "2" else OTASlotBootID.slot_b,
                standby_slot=OTASlotBootID.slot_a if _active_partid == "3" else OTASlotBootID.slot_b,
            )
            # fmt: on


@contextlib.contextmanager
def _prepare_chroot_env(target_slot_mp: Path, *, boot_source: str):
    mounts: dict[Path, str] = {
        target_slot_mp / "proc": "/proc",
        target_slot_mp / "sys": "/sys",
        target_slot_mp / "dev": "/dev",
        target_slot_mp / "boot": boot_source,
    }
    try:
        for _mp, _src in mounts.items():
            cmdhelper.mount(_src, _mp, options=["bind"])
        yield
        # NOTE: passthrough the mount failure to caller
    finally:
        for _mp in mounts:
            cmdhelper.umount(_mp, raise_exception=False)


@dataclass
class _BootFiles:
    kernel_ver: str
    kernel: Path
    initrd: Path


class _GrubBootControl:
    """Low-level boot control implementation for grub boot control."""

    def __init__(self) -> None:
        self.boot_slots = boot_slots = ABPartitionDetector.detect_boot_slots()
        if boot_slots.is_uefi:
            Path(cfg.EFI_DPATH).mkdir(exist_ok=True)

        # TODO: recovery and migration
        if _require_resetup := not self._detect_boot_control_setup():
            pass

        self.initialized = _require_resetup

    def _bootstrap_retrieve_booted_kernel_initramfs(self) -> _BootFiles:
        """Detect the current booted kernel and initramfs.

        We assume that the initramfs lives under the same folder of kernel.
        """
        _boot_image_ma = re.search(
            r"BOOT_IMAGE=(?P<kernel>[^\s]+)", read_str_from_file("/proc/cmdline")
        )
        if not _boot_image_ma:
            raise GrubBootControllerError(
                "failed to bootstrap: cannot find the booted kernel"
            )

        _boot_image = _boot_image_ma.group("kernel").strip()
        _boot_image = Path(_boot_image).resolve()
        _kernel_ver = _boot_image.name.replace(VMLINUZ_PREFIX, "", 1)

        _initrd_image = _boot_image.parent / f"{INITRD_PREFIX}{_kernel_ver}"
        if not _initrd_image.is_file():
            raise GrubBootControllerError(f"initramfs for {_kernel_ver=} not found!")

        return _BootFiles(_kernel_ver, _boot_image, _initrd_image)

    def _bootstrap_setup_boot_slot_dir(
        self, _boot_files: _BootFiles, slot_id: OTASlotBootID
    ) -> None:
        """Setup the boot slot dir for the target slot."""
        _slot_boot_dir = self.get_boot_slot_dir(slot_id)
        remove_file(_slot_boot_dir)

        _slot_boot_dir.mkdir(exist_ok=True)
        _kernel, _initrd = _boot_files.kernel, _boot_files.initrd
        copyfile_atomic(_kernel, _slot_boot_dir / _kernel.name)
        copyfile_atomic(_initrd, _slot_boot_dir / _initrd.name)

    def _bootstrap_setup_rootfs_for_ota_boot(self, slot_mp: Path):
        _current_slot_info = self.get_slot_info(self.boot_slots.current_slot)
        self.setup_slot_rootfs_for_ota_boot(
            slot_fsuuid=_current_slot_info.uuid, slot_mp=slot_mp
        )

    def _bootstrap_setup_boot_cfg(self, _boot_files: _BootFiles, slot_mp: Path):
        self.setup_ota_boot_cfg_for_slot(
            _boot_files.kernel_ver,
            slot_id=self.boot_slots.current_slot,
            slot_mp=slot_mp,
        )
    def _bootstrap_boot_control(self):
        """Bootstrap(migrate) from non-OTA setup system.

        Things to do:
        1.
        """

    @staticmethod
    def _read_fstab_dict(_in: str) -> dict[str, re.Match]:
        """Return {mount_point: match} for valid fstab entries only"""
        # Strictly match valid fstab entry lines
        fstab_entry_pa = re.compile(
            r"^\s*(?P<file_system>\S+)\s+"
            r"(?P<mount_point>\S+)\s+"
            r"(?P<type>\S+)\s+"
            r"(?P<options>\S+)\s+"
            r"(?P<dump>\d+)\s+(?P<pass>\d+)\s*$"
        )

        entries = {}
        for line in _in.splitlines():
            if m := fstab_entry_pa.match(line):
                entries[m.group("mount_point")] = m
        return entries

    def _detect_grub_version(self, _slot_mp: Path) -> str:
        """Detect the installed grub version from `_slot_mp`."""
        _res = subprocess_run_wrapper(
            ["grub-mkconfig", "--version"],
            check=False,
            check_output=True,
            chroot=_slot_mp,
        )

        if _res.returncode != 0 or not (_stdout := _res.stdout.decode()):
            logger.warning(
                f"failed to detect grub installation version: {_res.stderr.decode()=}"
            )
            return "unknown_grub_version"

        # e.g. "grub-mkconfig (GRUB) 2.12-1ubuntu7.3"
        if _ma := re.search(r"\(GRUB\)\s+(?P<ver>[\w.\-]+)", _stdout):
            return _ma.group("ver")

        logger.warning(f"irregular grub version string: {_stdout=}")
        return _stdout

    def _detect_boot_control_setup(self) -> bool:
        """Detect whether the ECU has grub boot control properly setup."""
        _grub_cfg = Path(boot_cfg.GRUB_CFG_FPATH)
        if _grub_cfg.is_symlink():
            return False  # old grub boot control setup
        if not OTAManagedCfg.validate_managed_config(read_str_from_file(_grub_cfg)):
            return False  # /boot/grub/grub.cfg used to be managed by us, but being modified
        return True

    def _grub_mkconfig_on_mp(self, _slot_mp: Path, _boot_source: str) -> str:
        with _prepare_chroot_env(_slot_mp, boot_source=_boot_source):
            try:
                _res = subprocess_run_wrapper(
                    ["grub-mkconfig"], check=True, check_output=True, chroot=_slot_mp
                )

                return _res.stdout.decode()
            except CalledProcessError as e:
                logger.exception(f"grub-mkconfig on {_slot_mp=} failed")
                raise GrubBootControllerError(
                    f"grub-mkconfig on {_slot_mp=} failed"
                ) from e

    def _generate_fstab(
        self, *, base_fstab: str, reference_fstab: str | None = None, slot_fsuuid: str
    ) -> str:
        """Rebuild standby fstab using valid mount entries only.

        - Keep only valid mount entries (skip comments, invalid or broken lines)
        - Always include '/', '/boot', and '/boot/efi' from active slot
        - Replace root ('/') UUID with standby slot UUID
        - Drop all other comments or extra metadata lines
        """
        slot_uuid_str = f"UUID={slot_fsuuid}"

        # active_dict
        reference_dict = (
            self._read_fstab_dict(reference_fstab) if reference_fstab else None
        )
        # standby_dict
        base_dict = self._read_fstab_dict(base_fstab)

        merged: list[str] = []

        # These special base mount points(/, /boot, /boot/efi) are created by USB Installer and not in project settings,
        # so we need to preserve them from active slot's fstab.
        # Reference: https://tier4.atlassian.net/browse/T4DEV-39187

        # Always include root ("/")
        if reference_dict and cfg.CANONICAL_ROOT in reference_dict:
            ma = reference_dict[cfg.CANONICAL_ROOT]
            merged.append("\t".join([slot_uuid_str] + list(ma.groups())[1:]))
        else:
            merged.append(f"{slot_uuid_str}\t/\text4\terrors=remount-ro\t0\t1")

        # Add /boot and /boot/efi from active if available
        # NOTE: order matters! /boot MUST be mounted before /boot/efi mounted!
        if reference_dict and (_boot_mp := cfg.BOOT_DPATH) in reference_dict:
            merged.append("\t".join(reference_dict[_boot_mp].groups()))
        else:
            boot_part_uuid_str = f"UUID={self.boot_slots.boot_partition.uuid}"
            merged.append(f"{boot_part_uuid_str}\t/boot\text4\tdefaults\t0\t1")

        # no need to add EFI mount for non-UEFI system
        if self.boot_slots.efi_partition:
            if reference_dict and (_boot_eif_mp := cfg.EFI_DPATH) in reference_dict:
                merged.append("\t".join(reference_dict[_boot_eif_mp].groups()))
            else:
                efi_part_uuid_str = f"UUID={self.boot_slots.efi_partition.uuid}"
                merged.append(f"{efi_part_uuid_str}\t/boot/efi\tvfat\tdefaults\t0\t1")

        # Append all remaining valid entries from standby (except /, /boot, /boot/efi)
        for mp, ma in base_dict.items():
            if mp not in (cfg.CANONICAL_ROOT, cfg.BOOT_DPATH, cfg.EFI_DPATH):
                merged.append("\t".join(ma.groups()))

        merged.append("")  # add a new line at the end of file
        return "\n".join(merged)

    def _update_grub_default(self, _in: str) -> str:
        """Read in grub_default str and return updated one.

        Update rules:
        1. predefined default_kvp has the highest priority, and overrides any
           presented options in the original grub_default,
        2. option that specified multiple times will be merged into one,
           and the latest specified value will be used, or predefined default value will
           be used if such value defined.
        """
        res_kvp: dict[str, str] = {}
        for option_line in _in.splitlines():
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
        res_kvp.update(GRUB_DEFAULT_OPTIONS)
        for _blacklist_option in GRUB_BLACKLIST_OPTIONS:
            res_kvp.pop(_blacklist_option, None)

        res = [OTA_MANAGED_CFG_HEADER]
        res.extend(f"{k}={v}" for k, v in res_kvp.items())
        res.append("")  # add a new line at the end of the file
        return "\n".join(res)

    # API

    def get_slot_info(self, slot_id: OTASlotBootID) -> _SlotInfo:
        return (
            self.boot_slots.slot_a
            if slot_id == OTASlotBootID.slot_a
            else self.boot_slots.slot_b
        )

    def get_boot_cfg_fpath(self, slot_id: OTASlotBootID) -> Path:
        """`/boot/grub/ota-slot_<a/b>.cfg`"""
        return Path(boot_cfg.GRUB_DIR) / f"{slot_id}{boot_cfg.SLOT_BOOT_CFG_SUFFIX}"

    def get_boot_slot_dir(self, slot_id: OTASlotBootID) -> Path:
        """`/boot/ota-slot_<a/b>`"""
        return Path(boot_cfg.BOOT_DPATH) / slot_id

    def detect_slot_kernel_ver(self, _slot_mp: Path) -> str:
        """Detect the kernel version by looking at `<_slot_mp>/boot` folder.

        Expect only one version of kernel installed.
        If multiple kernel version found, will pick the first found one.
        """
        _slot_boot = _slot_mp / "boot"
        for _vmlinuz in _slot_boot.glob(f"{VMLINUZ_PREFIX}*"):
            _kernel_ver = _vmlinuz.name.replace(VMLINUZ_PREFIX, "", 1)

            if (_slot_boot / f"{INITRD_PREFIX}{_kernel_ver}").is_file():
                return _kernel_ver
        else:
            raise GrubBootControllerError(
                f"no kernel installation found from {_slot_mp}!"
            )

    def setup_boot_slot_dir(
        self, _kernel_ver: str, *, slot_id: OTASlotBootID, slot_mp: Path
    ) -> None:
        """Copy the boot files from slot rootfs to boot slot dir."""
        # prepare the boot slot dir
        # NOTE(20260310): IMPORTANT! For backward compatibility, also copy
        #                 the boot files to the root of /boot folder.
        #                 This is for old grub boot control bootstraps itself.
        _boot_slot_dir = self.get_boot_slot_dir(slot_id)
        for f in (slot_mp / "boot").glob(f"*{_kernel_ver}"):
            if f.is_file() and not f.is_symlink():
                copyfile_atomic(f, _boot_slot_dir / f.name)
                copyfile_atomic(f, Path(boot_cfg.BOOT_DPATH) / f.name)

    def setup_slot_rootfs_for_ota_boot(
        self, *, slot_fsuuid: str, slot_mp: Path, reference_fstab: str | None = None
    ) -> None:
        """Prepare the slot for OTA boot at `slot_mp`.

        Things to do:
        1. update fstab at the slot rootfs.
        2. update /etc/default/grub at the slot rootfs.
        3. inject /etc/grub.d/30_ota hook at the slot rootfs.
        """
        # update the fstab, base_fstab will be the slot we update,
        #   reference_fstab will be from the sibling slot.
        # e.g., base_fstab(standby_slot), reference_fstab(active_slot) when doing OTA.
        _fstab_fpath = replace_root(
            boot_cfg.FSTAB_FILE_PATH, cfg.CANONICAL_ROOT, slot_mp
        )
        write_str_to_file_atomic(
            _fstab_fpath,
            self._generate_fstab(
                base_fstab=read_str_from_file(_fstab_fpath),
                reference_fstab=reference_fstab,
                slot_fsuuid=slot_fsuuid,
            ),
        )

        # update the /etc/default/grub
        _grub_default_fpath = replace_root(
            boot_cfg.DEFAULT_GRUB_PATH, cfg.CANONICAL_ROOT, slot_mp
        )
        write_str_to_file_atomic(
            _grub_default_fpath,
            self._update_grub_default(read_str_from_file(_grub_default_fpath)),
        )

        # inject the /etc/grub.d/30_ota hook and set it as executable
        _hook_dpath = replace_root(
            boot_cfg.GRUB_HOOKS_DPATH, cfg.CANONICAL_ROOT, slot_mp
        )
        _hook_fpath = Path(_hook_dpath) / boot_cfg.OTA_GRUB_HOOK_FNAME
        write_str_to_file_atomic(_hook_fpath, boot_cfg.OTA_GRUB_HOOK)
        os.chmod(_hook_fpath, 0o750)

    def setup_ota_boot_cfg_for_slot(
        self,
        _kernel_ver: str,
        *,
        slot_id: OTASlotBootID,
        slot_mp: Path,
    ) -> None:
        """Generate boot cfg for `slot_mp` with `slot_id` and write to `boot_cfg_fpath`.

        This method should be called AFTER `setup_boot_slot_dir` and `setup_slot_rootfs_for_ota_boot`.
        """
        _slot_boot_dir = Path(boot_cfg.BOOT_DPATH) / slot_id
        _raw_grub_mkconfig = self._grub_mkconfig_on_mp(slot_mp, str(_slot_boot_dir))
        _boot_cfg = _BootMenuEntry.generate_menuentry(
            _raw_grub_mkconfig, slot_boot_id=slot_id, kernel_ver=_kernel_ver
        )
        write_str_to_file_atomic(
            Path(boot_cfg.GRUB_DIR) / f"{slot_id}{boot_cfg.SLOT_BOOT_CFG_SUFFIX}",
            _boot_cfg.raw_entry,
        )

    def grub_reboot_to_standby(self) -> None:
        _standby_slot = self.boot_slots.standby_slot
        try:
            subprocess_run_wrapper(
                ["grub-reboot", _standby_slot],
                check=True,
                check_output=True,
                chroot=_env.get_dynamic_client_chroot_path(),
            )
        except CalledProcessError:
            logger.exception(f"failed to grub-reboot to {_standby_slot}")
            raise GrubBootControllerError(
                f"`grub-reboot {_standby_slot}` failed"
            ) from None

    def finalize_update_switch_boot(self) -> Literal[True]:
        _current_slot = self.boot_slots.current_slot
        try:
            subprocess_run_wrapper(
                ["grub-set-default", _current_slot],
                check=True,
                check_output=True,
                chroot=_env.get_dynamic_client_chroot_path(),
            )
            return True
        except CalledProcessError:
            logger.exception(f"failed to grub-set-default to {_current_slot}")
            raise GrubBootControllerError(
                f"`grub-set-default {_current_slot}` failed"
            ) from None


class GrubBootController(BootControllerBase):
    def __init__(self) -> None:
        try:
            self._boot_control = boot_control = _GrubBootControl()

            self._boot_slots = boot_slots = boot_control.boot_slots
            self._current_slot = current_slot = boot_slots.current_slot
            self._standby_slot = standby_slot = boot_slots.standby_slot

            self._mp_control = SlotMountHelper(
                standby_slot_dev=boot_control.get_slot_info(standby_slot).dev,
                standby_slot_mount_point=cfg.STANDBY_SLOT_MNT,
                active_rootfs=cfg.ACTIVE_ROOT,
                active_slot_mount_point=cfg.ACTIVE_SLOT_MNT,
            )
            # NOTE: boot slot dir stores both boot files and OTA status files.
            self._ota_status_control = OTAStatusFilesControl(
                active_slot=current_slot,
                standby_slot=standby_slot,
                current_ota_status_dir=boot_control.get_boot_slot_dir(current_slot),
                standby_ota_status_dir=boot_control.get_boot_slot_dir(standby_slot),
                finalize_switching_boot=self._boot_control.finalize_update_switch_boot,
                # NOTE(20230904): if boot control is initialized(i.e., migrate from non-ota booted system),
                #                 force initialize the ota_status files.
                force_initialize=self._boot_control.initialized,
            )
        except Exception as e:
            _err_msg = f"failed on start grub boot controller: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlStartupFailed(_err_msg, module=__name__) from e

    # API

    @property
    def bootloader_type(self) -> str:
        return boot_cfg.BOOTLOADER

    def _pre_update_prepare_standby(self, *, erase_standby: bool) -> None:
        """
        Override the base's `_pre_update_prepare_standby`.
        """
        _standby_slot_info = self._boot_control.get_slot_info(self._standby_slot)
        self._mp_control.prepare_standby_dev(
            erase_standby=erase_standby,
            fsuuid=_standby_slot_info.uuid,
        )

    def _pre_update_platform_specific(
        self, *, standby_as_ref: bool, erase_standby: bool
    ) -> None:
        """GRUB-specific pre-update: cleanup standby ota_partition folder."""
        _boot_slot_dir = self._boot_control.get_boot_slot_dir(self._standby_slot)
        remove_file(_boot_slot_dir)
        _boot_slot_dir.mkdir(parents=True)

    def _post_update_platform_specific(self, *, update_version: str) -> None:
        """GRUB-specific post-update: update fstab, copy boot files, and reboot to standby."""
        _kernel_ver = self._boot_control.detect_slot_kernel_ver(
            self._mp_control.standby_slot_mount_point
        )
        _standby_slot_mp = self._mp_control.standby_slot_mount_point
        _standby_slot_info = self._boot_control.get_slot_info(self._standby_slot)
        _standby_slot_id = self._boot_slots.standby_slot

        # NOTE: order of function calls matters!
        self._boot_control.setup_boot_slot_dir(
            _kernel_ver, slot_id=_standby_slot_id, slot_mp=_standby_slot_mp
        )
        self._boot_control.setup_slot_rootfs_for_ota_boot(
            slot_fsuuid=_standby_slot_info.uuid,
            slot_mp=_standby_slot_mp,
            reference_fstab=read_str_from_file(
                replace_root(
                    boot_cfg.FSTAB_FILE_PATH,
                    cfg.CANONICAL_ROOT,
                    self._mp_control.active_slot_mount_point,
                )
            ),
        )
        self._boot_control.setup_ota_boot_cfg_for_slot(
            _kernel_ver, slot_id=_standby_slot_id, slot_mp=_standby_slot_mp
        )

        self._boot_control.grub_reboot_to_standby()

    def finalizing_update(self, *, chroot: str | None = None) -> NoReturn:
        cmdhelper.reboot(chroot=chroot)
