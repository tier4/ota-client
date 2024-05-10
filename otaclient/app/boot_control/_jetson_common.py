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

from otaclient.app.common import write_str_to_file_sync

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
        if isinstance(_in, str):
            major_ver, major_rev, minor_rev = _in[1:].split(".")
            return cls(int(major_ver), int(major_rev), int(minor_rev))
        raise ValueError(f"expect str or BSPVersion instance, get {type(_in)}")

    @staticmethod
    def dump(to_export: BSPVersion) -> str:
        """Dump BSPVersion to string as "Rxx.yy.z"."""
        return f"R{to_export.major_ver}.{to_export.major_rev}.{to_export.minor_rev}"


class FirmwareBSPVersion(BaseModel):
    """
    BSP version string schema: Rxx.yy.z
    """

    BSPVersionStr = Annotated[
        BSPVersion,
        BeforeValidator(BSPVersion.parse),
        PlainSerializer(BSPVersion.dump, return_type=str),
    ]
    """BSPVersion in string representation, used by FirmwareBSPVersion model."""

    slot_a: Optional[BSPVersionStr] = None
    slot_b: Optional[BSPVersionStr] = None


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
    NVBootctrlTarget = Literal["bootloader", "rootfs"]

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
        return

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
        self, current_firmware_bsp_vf: Path, standby_firmware_bsp_vf: Path
    ) -> None:
        self._current_fw_bsp_vf = current_firmware_bsp_vf
        self._standby_fw_bsp_vf = standby_firmware_bsp_vf

        self._version = FirmwareBSPVersion()
        try:
            self._version = _version = FirmwareBSPVersion.model_validate_json(
                self._current_fw_bsp_vf.read_text()
            )
            logger.info(f"firmware_version: {_version}")
        except Exception as e:
            logger.warning(
                f"invalid or missing firmware_bsp_verion file, removed: {e!r}"
            )
            self._current_fw_bsp_vf.unlink(missing_ok=True)

    def write_current_firmware_bsp_version(self) -> None:
        """Write firmware_bsp_version from memory to firmware_bsp_version file."""
        write_str_to_file_sync(self._current_fw_bsp_vf, self._version.model_dump_json())

    def write_standby_firmware_bsp_version(self) -> None:
        """Write firmware_bsp_version from memory to firmware_bsp_version file."""
        write_str_to_file_sync(self._standby_fw_bsp_vf, self._version.model_dump_json())

    def get_version_by_slot(self, slot_id: SlotID) -> Optional[BSPVersion]:
        """Get <slot_id> slot's firmware version from memory."""
        if slot_id == "0":
            return self._version.slot_a
        return self._version.slot_b

    def set_version_by_slot(
        self, slot_id: SlotID, version: Optional[BSPVersion]
    ) -> None:
        """Set <slot_id> slot's firmware version into memory."""
        if slot_id == "0":
            self._version.slot_a = version
        else:
            self._version.slot_b = version


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
