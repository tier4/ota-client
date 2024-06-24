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

from otaclient.app.boot_control._common import CMDHelperFuncs
from otaclient_common.common import copytree_identical, write_str_to_file_sync

logger = logging.getLogger(__name__)


class SlotID(str):
    """slot_id for A/B slots.

    On NVIDIA Jetson device, slot_a has slot_id=0, slot_b has slot_id=1.
        For slot_a, the slot partition name suffix is "" or "_a".
        For slot_b, the slot partition name suffix is "_b".
    """

    VALID_SLOTS = ["0", "1"]

    def __new__(cls, _in: str | Self) -> Self:
        if isinstance(_in, cls):
            return _in
        if _in in cls.VALID_SLOTS:
            return str.__new__(cls, _in)
        raise ValueError(f"{_in=} is not valid slot num, should be '0' or '1'.")


class BSPVersion(NamedTuple):
    """BSP version in NamedTuple representation.

    Example: R32.6.1 -> (32, 6, 1)
    """

    major_ver: int
    major_rev: int
    minor_rev: int

    @classmethod
    def parse(cls, _in: str | BSPVersion | Any) -> Self:
        """Parse "Rxx.yy.z string into BSPVersion."""
        if isinstance(_in, cls):
            return _in
        if isinstance(_in, str) and len(_split := _in[1:].split(".")) == 3:
            major_ver, major_rev, minor_rev = _split
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
"""BSPVersion in string representation, used by FirmwareBSPVersion model."""


class FirmwareBSPVersion(BaseModel):
    """
    BSP version string schema: Rxx.yy.z
    """

    slot_a: Optional[BSPVersionStr] = None
    slot_b: Optional[BSPVersionStr] = None

    def set_by_slot(self, slot_id: SlotID, ver: BSPVersion | None) -> None:
        if slot_id == SlotID("0"):
            self.slot_a = ver
        elif slot_id == SlotID("1"):
            self.slot_b = ver
        else:
            raise ValueError(f"invalid slot_id: {slot_id}")

    def get_by_slot(self, slot_id: SlotID) -> BSPVersion | None:
        if slot_id == SlotID("0"):
            return self.slot_a
        elif slot_id == SlotID("1"):
            return self.slot_b
        else:
            raise ValueError(f"invalid slot_id: {slot_id}")


NVBootctrlTarget = Literal["bootloader", "rootfs"]


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
        check_output,
        target: Optional[NVBootctrlTarget] = None,
    ) -> Any:
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
        """Prints currently running SLOT."""
        cmd = "get-current-slot"
        res = cls._nvbootctrl(cmd, check_output=True, target=target)
        assert isinstance(res, str), f"invalid output from get-current-slot: {res}"
        return SlotID(res.strip())

    @classmethod
    def get_standby_slot(cls, *, target: Optional[NVBootctrlTarget] = None) -> SlotID:
        """Prints standby SLOT.

        NOTE: this method is implemented with nvbootctrl get-current-slot.
        """
        current_slot = cls.get_current_slot(target=target)
        return SlotID("0") if current_slot == "1" else SlotID("1")

    @classmethod
    def set_active_boot_slot(
        cls, slot_id: SlotID, *, target: Optional[NVBootctrlTarget] = None
    ) -> None:
        """On next boot, load and execute SLOT."""
        cmd = "set-active-boot-slot"
        return cls._nvbootctrl(cmd, SlotID(slot_id), check_output=False, target=target)

    @classmethod
    def dump_slots_info(cls, *, target: Optional[NVBootctrlTarget] = None) -> str:
        """Prints info for slots."""
        cmd = "dump-slots-info"
        return cls._nvbootctrl(cmd, target=target, check_output=True)


class FirmwareBSPVersionControl:
    """firmware_bsp_version ota-status file for tracking firmware version.

    The firmware BSP version is stored in /boot/ota-status/firmware_bsp_version json file,
        tracking the firmware BSP version for each slot.

    Each slot should keep the same firmware_bsp_version file, this file is passed to standby slot
        during OTA update.
    """

    def __init__(
        self,
        current_slot: SlotID,
        current_slot_firmware_bsp_ver: BSPVersion | None = None,
        *,
        current_firmware_bsp_vf: Path,
        standby_firmware_bsp_vf: Path,
    ) -> None:
        self._current_fw_bsp_vf = current_firmware_bsp_vf
        self._standby_fw_bsp_vf = standby_firmware_bsp_vf

        self._version = FirmwareBSPVersion()
        try:
            self._version = FirmwareBSPVersion.model_validate_json(
                self._current_fw_bsp_vf.read_text()
            )
        except Exception as e:
            logger.warning(f"invalid or missing firmware_bsp_verion file: {e!r}")
            self._current_fw_bsp_vf.unlink(missing_ok=True)

        # NOTE: only check the standby slot's firmware BSP version info from file,
        #   for current slot, always trust the value from nvbootctrl.
        self._version.set_by_slot(current_slot, current_slot_firmware_bsp_ver)
        logger.info(f"loading firmware bsp version completed: {self._version}")

    def write_to_currnet_slot(self) -> None:
        """Write firmware_bsp_version from memory to firmware_bsp_version file."""
        write_str_to_file_sync(self._current_fw_bsp_vf, self._version.model_dump_json())

    def write_to_standby_slot(self) -> None:
        """Write firmware_bsp_version from memory to firmware_bsp_version file."""
        write_str_to_file_sync(self._standby_fw_bsp_vf, self._version.model_dump_json())

    def get_version_by_slot(self, slot_id: SlotID) -> Optional[BSPVersion]:
        """Get <slot_id> slot's firmware version from memory."""
        return self._version.get_by_slot(slot_id)

    def set_version_by_slot(
        self, slot_id: SlotID, version: Optional[BSPVersion]
    ) -> None:
        """Set <slot_id> slot's firmware version into memory."""
        self._version.set_by_slot(slot_id, version)


BSP_VER_PA = re.compile(
    (
        r"# R(?P<major_ver>\d+) \(\w+\), REVISION: (?P<major_rev>\d+)\.(?P<minor_rev>\d+), "
        r"GCID: (?P<gcid>\d+), BOARD: (?P<board>\w+), EABI: (?P<eabi>\w+)"
    )
)
"""Example: # R32 (release), REVISION: 6.1, GCID: 27863751, BOARD: t186ref, EABI: aarch64, DATE: Mon Jul 26 19:36:31 UTC 2021 """


def parse_bsp_version(nv_tegra_release: str) -> BSPVersion:
    """Parse BSP version from contents of /etc/nv_tegra_release.

    see https://developer.nvidia.com/embedded/jetson-linux-archive for BSP version history.
    """
    ma = BSP_VER_PA.match(nv_tegra_release)
    assert ma, f"invalid nv_tegra_release content: {nv_tegra_release}"
    return BSPVersion(
        int(ma.group("major_ver")),
        int(ma.group("major_rev")),
        int(ma.group("minor_rev")),
    )


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
    internal_emmc_mp: Path | str,
    internal_emmc_devpath: Path | str,
    standby_slot_boot_dirpath: Path | str,
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
