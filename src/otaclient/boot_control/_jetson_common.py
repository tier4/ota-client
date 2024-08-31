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
"""Jetson device boot control implementation common.

This module is shared by jetson-cboot and jetson-uefi bootloader type.
"""


from __future__ import annotations

import logging
import re
import subprocess
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple, Optional

from pydantic import BaseModel, BeforeValidator, PlainSerializer
from typing_extensions import Annotated, Literal, Self

from otaclient_common import replace_root
from otaclient_common.common import copytree_identical, write_str_to_file_sync
from otaclient_common.typing import StrOrPath

from ._common import CMDHelperFuncs
from .configs import jetson_common_cfg

logger = logging.getLogger(__name__)


class SlotID(str):
    """slot_id for A/B slots.

    slot_a has slot_id=0, slot_b has slot_id=1.
    """

    VALID_SLOTS = ["0", "1"]
    VALID_SLOTS_CHAR = ["A", "B"]

    def __new__(cls, _in: str | Self) -> Self:
        if isinstance(_in, cls):
            return _in
        if _in in cls.VALID_SLOTS:
            return str.__new__(cls, _in)
        if _in in cls.VALID_SLOTS_CHAR:
            return str.__new__(cls, "0") if _in == "A" else str.__new__(cls, "1")
        raise ValueError(
            f"{_in=} is not valid slot num, should be '0'('A') or '1'('B')."
        )


SLOT_A, SLOT_B = SlotID("0"), SlotID("1")
SLOT_FLIP = {SLOT_A: SLOT_B, SLOT_B: SLOT_A}
SLOT_PAR_MAP = {SLOT_A: 1, SLOT_B: 2}
"""SLOT_A: 1, SLOT_B: 2"""

BSP_VERSION_STR_PA = re.compile(
    r"[rR]?(?P<major_ver>\d+)\.(?P<major_rev>\d+)\.(?P<minor_rev>\d+)"
)


class BSPVersion(NamedTuple):
    """BSP version in NamedTuple representation.

    Example: R32.6.1 -> (32, 6, 1)
    """

    major_ver: int
    major_rev: int
    minor_rev: int

    @classmethod
    def parse(cls, _in: str | BSPVersion | Any) -> Self:
        """Parse BSP version string into BSPVersion.

        Raises:
            ValueError on invalid input.
        """
        if isinstance(_in, cls):
            return _in
        if isinstance(_in, str):
            ma = BSP_VERSION_STR_PA.match(_in)
            if not ma:
                raise ValueError(f"not a valid bsp version string: {_in}")

            major_ver, major_rev, minor_rev = (
                ma.group("major_ver"),
                ma.group("major_rev"),
                ma.group("minor_rev"),
            )
            return cls(int(major_ver), int(major_rev), int(minor_rev))
        raise ValueError(f"expect str or BSPVersion instance, get {type(_in)}")

    def dump(self) -> str:
        """Dump BSPVersion to string as "Rxx.yy.z"."""
        return f"R{self.major_ver}.{self.major_rev}.{self.minor_rev}"


BSPVersionStr = Annotated[
    BSPVersion,
    BeforeValidator(BSPVersion.parse),
    PlainSerializer(BSPVersion.dump, return_type=str),
]
"""BSPVersion in string representation."""


class SlotBSPVersion(BaseModel):
    """
    BSP version string schema: Rxx.yy.z
    """

    slot_a: Optional[BSPVersionStr] = None
    slot_b: Optional[BSPVersionStr] = None

    def set_by_slot(self, slot_id: SlotID, ver: BSPVersion | None) -> None:
        if slot_id == SLOT_A:
            self.slot_a = ver
        elif slot_id == SLOT_B:
            self.slot_b = ver
        else:
            raise ValueError(f"invalid slot_id: {slot_id}")

    def get_by_slot(self, slot_id: SlotID) -> BSPVersion | None:
        if slot_id == SLOT_A:
            return self.slot_a
        elif slot_id == SLOT_B:
            return self.slot_b
        else:
            raise ValueError(f"invalid slot_id: {slot_id}")


NVBootctrlTarget = Literal["bootloader", "rootfs"]


class NVBootctrlExecError(Exception):
    """Raised when nvbootctrl command execution failed."""


class NVBootctrlCommon:
    """Helper for calling nvbootctrl commands.

    Without -t option, the target will be bootloader by default.

    The NVBootctrlCommon class only contains methods that both exist on
        jetson-cboot and jetson-uefi, which are:

    1. get-current-slot
    2. set-active-boot-slot
    3. dump-slots-info

    Also, get-standby-slot is not a nvbootctrl and it is implemented using
        nvbootctrl get-current-slot command.
    """

    NVBOOTCTRL = "nvbootctrl"

    @classmethod
    def _nvbootctrl(
        cls,
        _cmd: str,
        _slot_id: Optional[SlotID] = None,
        *,
        check_output: bool,
        target: Optional[NVBootctrlTarget] = None,
    ) -> Any:
        """
        Raises:
            CalledProcessError on return code not equal to 0.
        """
        cmd = [cls.NVBOOTCTRL]
        if target:
            cmd.extend(["-t", target])
        cmd.append(_cmd)
        if _slot_id:
            cmd.append(str(_slot_id))

        res = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
        )
        if check_output:
            return res.stdout.decode()

    @classmethod
    def get_current_slot(cls, *, target: Optional[NVBootctrlTarget] = None) -> SlotID:
        """Prints currently running SLOT.

        Raises:
            NVBootctrlExecError on failed to get current slot.
        """
        cmd = "get-current-slot"
        try:
            res = cls._nvbootctrl(cmd, check_output=True, target=target)
            return SlotID(res.strip())
        except Exception as e:
            raise NVBootctrlExecError from e

    @classmethod
    def get_standby_slot(cls, *, target: Optional[NVBootctrlTarget] = None) -> SlotID:
        """Prints standby SLOT.

        NOTE: this method is implemented with nvbootctrl get-current-slot.

        Raises:
            NVBootctrlExecError on failed to get current slot.
        """
        current_slot = cls.get_current_slot(target=target)
        return SLOT_FLIP[current_slot]

    @classmethod
    def set_active_boot_slot(
        cls, slot_id: SlotID, *, target: Optional[NVBootctrlTarget] = None
    ) -> None:
        """On next boot, load and execute SLOT.

        Raises:
            NVBootctrlExecError on nvbootctrl call failed.
        """
        cmd = "set-active-boot-slot"
        try:
            return cls._nvbootctrl(
                cmd, SlotID(slot_id), check_output=False, target=target
            )
        except subprocess.CalledProcessError as e:
            raise NVBootctrlExecError from e

    @classmethod
    def dump_slots_info(cls, *, target: Optional[NVBootctrlTarget] = None) -> str:
        """Prints info for slots.

        Raises:
            NVBootctrlExecError on nvbootctrl call failed.
        """
        cmd = "dump-slots-info"
        try:
            return cls._nvbootctrl(cmd, target=target, check_output=True)
        except subprocess.CalledProcessError as e:
            raise NVBootctrlExecError from e


class FirmwareBSPVersionControl:
    """firmware_bsp_version ota-status file for tracking BSP version.

    The BSP version is stored in /boot/ota-status/firmware_bsp_version json file,
        tracking the firmware BSP version for each slot.
    NOTE that we only cares about firmware BSP version.

    Unfortunately, we don't have method to detect standby slot's firwmare vesrion,
        when the firmware_bsp_version file is not presented(typical case when we have newly setup device),
        we ASSUME that both slots are running the same BSP version of firmware.

    Each slot should keep the same firmware_bsp_version file, this file is passed to standby slot
        during OTA update as it.
    """

    def __init__(
        self,
        current_slot: SlotID,
        current_slot_bsp_ver: BSPVersion,
        *,
        current_bsp_version_file: Path,
    ) -> None:
        self.current_slot, self.standby_slot = current_slot, SLOT_FLIP[current_slot]

        self._version = SlotBSPVersion()
        try:
            self._version = SlotBSPVersion.model_validate_json(
                current_bsp_version_file.read_text()
            )
        except Exception as e:
            logger.warning(f"invalid or missing bsp_verion file: {e!r}")
            current_bsp_version_file.unlink(missing_ok=True)
            logger.warning(
                "assume standby slot is running the same version of firmware"
            )
            self._version.set_by_slot(self.standby_slot, current_slot_bsp_ver)

        # NOTE: only check the standby slot's firmware BSP version info from file,
        #   for current slot, always trust the value from nvbootctrl.
        self._version.set_by_slot(current_slot, current_slot_bsp_ver)

    def write_to_file(self, fw_bsp_fpath: StrOrPath) -> None:
        """Write firmware_bsp_version from memory to firmware_bsp_version file."""
        write_str_to_file_sync(fw_bsp_fpath, self._version.model_dump_json())

    @property
    def current_slot_bsp_ver(self) -> BSPVersion:
        assert (res := self._version.get_by_slot(self.current_slot))
        return res

    @current_slot_bsp_ver.setter
    def current_slot_bsp_ver(self, bsp_ver: BSPVersion | None):
        self._version.set_by_slot(self.current_slot, bsp_ver)

    @property
    def standby_slot_bsp_ver(self) -> BSPVersion | None:
        return self._version.get_by_slot(self.standby_slot)

    @standby_slot_bsp_ver.setter
    def standby_slot_bsp_ver(self, bsp_ver: BSPVersion | None):
        self._version.set_by_slot(self.standby_slot, bsp_ver)


NV_TEGRA_RELEASE_PA = re.compile(
    (
        r"# R(?P<major_ver>\d+) \(\w+\), REVISION: (?P<major_rev>\d+)\.(?P<minor_rev>\d+), "
        r"GCID: (?P<gcid>\d+), BOARD: (?P<board>\w+), EABI: (?P<eabi>\w+)"
    )
)
"""Example: # R32 (release), REVISION: 6.1, GCID: 27863751, BOARD: t186ref, EABI: aarch64, DATE: Mon Jul 26 19:36:31 UTC 2021 """


def parse_nv_tegra_release(nv_tegra_release: str) -> BSPVersion:
    """Parse BSP version from contents of /etc/nv_tegra_release.

    see https://developer.nvidia.com/embedded/jetson-linux-archive for BSP version history.
    """
    ma = NV_TEGRA_RELEASE_PA.match(nv_tegra_release)
    assert ma, f"invalid nv_tegra_release content: {nv_tegra_release}"
    return BSPVersion(
        int(ma.group("major_ver")),
        int(ma.group("major_rev")),
        int(ma.group("minor_rev")),
    )


def detect_rootfs_bsp_version(rootfs: StrOrPath) -> BSPVersion:
    """Detect rootfs BSP version on <rootfs>.

    Raises:
        ValueError on failed detection.

    Returns:
        BSPversion of the <rootfs>.
    """
    nv_tegra_release_fpath = replace_root(
        jetson_common_cfg.NV_TEGRA_RELEASE_FPATH,
        "/",
        rootfs,
    )
    try:
        return parse_nv_tegra_release(Path(nv_tegra_release_fpath).read_text())
    except Exception as e:
        _err_msg = f"failed to detect rootfs BSP version at: {rootfs}: {e!r}"
        logger.error(_err_msg)
        raise ValueError(_err_msg) from e


def get_nvbootctrl_conf_tnspec(nvbootctrl_conf: str) -> str | None:
    """Get the TNSPEC field from nv_boot_control conf file."""
    for line in nvbootctrl_conf.splitlines():
        if line.strip().startswith("TNSPEC"):
            _, tnspec = line.split(" ", maxsplit=1)
            return tnspec.strip()


def update_extlinux_cfg(_input: str, partuuid: str) -> str:
    """Update input exlinux text with input rootfs <partuuid_str>."""
    partuuid_str = f"PARTUUID={partuuid}"

    def _replace(ma: re.Match, repl: str):
        append_l: str = ma.group(0)
        if append_l.startswith("#"):
            return append_l
        res, n = re.compile(r"root=[\w\-=]*").subn(repl, append_l)
        if not n:  # this APPEND line doesn't contain root= placeholder
            res = f"{append_l} {repl}"

        return res

    _repl_func = partial(_replace, repl=f"root={partuuid_str}")
    return re.compile(r"\n\s*APPEND.*").sub(_repl_func, _input)


def copy_standby_slot_boot_to_internal_emmc(
    *,
    internal_emmc_mp: StrOrPath,
    internal_emmc_devpath: StrOrPath,
    standby_slot_boot_dirpath: StrOrPath,
) -> None:
    """Copy the standby slot's /boot to internal emmc dev.

    This method is involved when external rootfs is enabled, aligning with
        the behavior of the NVIDIA flashing script.

    WARNING: DO NOT call this method if we are not booted from external rootfs!
    NOTE: at the time this method is called, the /boot folder at
        standby slot rootfs MUST be fully setup!
    """
    internal_emmc_mp = Path(internal_emmc_mp)
    internal_emmc_mp.mkdir(exist_ok=True, parents=True)

    try:
        CMDHelperFuncs.umount(internal_emmc_devpath, raise_exception=False)
        CMDHelperFuncs.mount_rw(
            target=str(internal_emmc_devpath),
            mount_point=internal_emmc_mp,
        )
    except Exception as e:
        _msg = f"failed to mount standby internal emmc dev: {e!r}"
        logger.error(_msg)
        raise ValueError(_msg) from e

    try:
        dst = internal_emmc_mp / "boot"
        # copy the standby slot's boot folder to emmc boot dev
        copytree_identical(Path(standby_slot_boot_dirpath), dst)
    except Exception as e:
        _msg = f"failed to populate standby slot's /boot folder to standby internal emmc dev: {e!r}"
        logger.error(_msg)
        raise ValueError(_msg) from e
    finally:
        CMDHelperFuncs.umount(internal_emmc_mp, raise_exception=False)


def preserve_ota_config_files_to_standby(
    *, active_slot_ota_dirpath: Path, standby_slot_ota_dirpath: Path
) -> None:
    """Preserve /boot/ota to standby /boot folder."""
    if not active_slot_ota_dirpath.is_dir():  # basically this should not happen
        logger.warning(
            f"{active_slot_ota_dirpath} doesn't exist, skip preserve /boot/ota folder."
        )
        return
    # TODO: (20240411) reconsidering should we preserve /boot/ota?
    copytree_identical(active_slot_ota_dirpath, standby_slot_ota_dirpath)


def update_standby_slot_extlinux_cfg(
    *,
    active_slot_extlinux_fpath: Path,
    standby_slot_extlinux_fpath: Path,
    standby_slot_partuuid: str,
):
    """update standby slot's /boot/extlinux/extlinux.conf to update root indicator."""
    src = standby_slot_extlinux_fpath
    # if standby slot doesn't have extlinux.conf installed, use current booted
    #   extlinux.conf as template source.
    if not standby_slot_extlinux_fpath.is_file():
        logger.warning(
            f"{standby_slot_extlinux_fpath} doesn't exist, use active slot's extlinux file as template"
        )
        src = active_slot_extlinux_fpath

    write_str_to_file_sync(
        standby_slot_extlinux_fpath,
        update_extlinux_cfg(
            src.read_text(),
            standby_slot_partuuid,
        ),
    )


def detect_external_rootdev(parent_devpath: StrOrPath) -> bool:
    """Check whether the ECU is using external device as root device or not.

    Returns:
        True if device is booted from external NVMe SSD, False if device is booted
            from internal emmc device.
    """
    parent_devname = Path(parent_devpath).name
    if parent_devname.startswith(jetson_common_cfg.INTERNAL_EMMC_DEVNAME):
        logger.info(f"device boots from internal emmc: {parent_devpath}")
        return False
    logger.info(f"device boots from external device: {parent_devpath}")
    return True


def get_partition_devpath(parent_devpath: StrOrPath, partition_id: int) -> str:
    """Get partition devpath from <parent_devpath> and <partition_id>.

    For internal emmc like /dev/mmcblk0 with partition_id 1, we will get:
        /dev/mmcblk0p1
    For external NVMe SSD like /dev/nvme0n1 with partition_id 1, we will get:
        /dev/nvme0n1p1
    For other types of device, including USB drive, like /dev/sda with partition_id 1,
        we will get: /dev/sda1
    """
    parent_devpath = str(parent_devpath).strip().rstrip("/")

    parent_devname = Path(parent_devpath).name
    if parent_devname.startswith(
        jetson_common_cfg.MMCBLK_DEV_PREFIX
    ) or parent_devname.startswith(jetson_common_cfg.NVMESSD_DEV_PREFIX):
        return f"{parent_devpath}p{partition_id}"
    if parent_devname.startswith(jetson_common_cfg.SDX_DEV_PREFIX):
        return f"{parent_devpath}{partition_id}"

    logger.warning(f"unexpected {parent_devname=}, treat it the same as sdx type")
    return f"{parent_devpath}{partition_id}"
