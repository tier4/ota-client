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
"""Consts, helper classes and functions for grub boot controller."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Dict, Optional

from typing_extensions import Self

from _otaclient_version import version
from otaclient_common._typing import StrEnum

from .configs import grub_new_cfg as boot_cfg

OTA_MANAGED_CFG_HEADER = (
    "# OTAClient managed configuration file, DO NOT EDIT!\n"
    "# Manual edits to this file will NOT be preserved across OTA!\n"
)


class GrubBootControllerError(Exception):
    """Grub boot controller internal used exception."""


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
    checksum: str = field(init=False)
    otaclient_version: str = version

    def __post_init__(self) -> None:
        self.raw_contents = self.raw_contents.strip()
        self.checksum = (
            f"sha256:{hashlib.sha256(self.raw_contents.encode()).hexdigest()}"
        )

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
        try:
            _algorithm, _expected_digest = checksum.split(":", 1)
            _hasher = hashlib.new(_algorithm, content.encode())
        except ValueError:
            return

        if _hasher.hexdigest() != _expected_digest:
            return

        return cls(
            raw_contents=content,
            grub_version=grub_version,
            otaclient_version=otaclient_version,
        )

    def export(self) -> str:
        _footer = (
            f"{self.FOOTER_HEAD}"
            f"# otaclient_version: {self.otaclient_version}\n"
            f"# grub_version: {self.grub_version}\n"
            f"# checksum: {self.checksum}\n"
            f"{self.FOOTER_TAIL}"
        )
        return f"{self.HEADER}{self.raw_contents}\n{_footer}"


class OTASlotBootID(StrEnum):
    slot_a = f"{boot_cfg.OTA_BOOT_SLOT_BASE}{boot_cfg.SLOT_A_SUFFIX}"
    slot_b = f"{boot_cfg.OTA_BOOT_SLOT_BASE}{boot_cfg.SLOT_B_SUFFIX}"


@dataclass
class SlotInfo:
    dev: str
    uuid: str
    slot_id: Optional[OTASlotBootID] = None

    @classmethod
    def from_partinfo(
        cls, _partinfo: PartitionInfo, _slot_id: OTASlotBootID | None = None
    ) -> Self:
        return cls(slot_id=_slot_id, dev=_partinfo.dev, uuid=_partinfo.uuid)


@dataclass
class ABPartition:
    is_uefi: bool

    boot_partition: SlotInfo
    slot_a: SlotInfo
    slot_b: SlotInfo
    current_slot: OTASlotBootID
    standby_slot: OTASlotBootID

    old_slot_id_mapping: Dict[OTASlotBootID, str]
    efi_partition: Optional[SlotInfo] = None


@dataclass
class PartitionInfo:
    dev: str
    uuid: str
    parttype: str


@dataclass
class BootFiles:
    kernel_ver: str
    kernel: Path
    initrd: Path


def read_fstab_dict(_in: str) -> dict[str, re.Match]:
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
        line = line.strip()
        if line.startswith("#"):
            continue

        if m := fstab_entry_pa.match(line):
            entries[m.group("mount_point")] = m
    return entries
