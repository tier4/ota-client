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

import re
from dataclasses import dataclass
from typing import ClassVar, Generator

from typing_extensions import Self

from _otaclient_version import version
from otaclient_common._typing import StrEnum

from .configs import grub_new_cfg

VMLINUZ_SUFFIX = "vmlinuz-"
INITRD_SUFFIX = "initrd.img-"

GRUB_DEFAULT_OPTIONS = {
    "GRUB_TIMEOUT_STYLE": "menu",
    "GRUB_TIMEOUT": "0",
    "GRUB_DISABLE_SUBMENU": "y",
    "GRUB_DISABLE_OS_PROBER": "true",
    "GRUB_DISABLE_RECOVERY": "true",
    "GRUB_DEFAULT": "saved",
    "-GRUB_SAVEDEFAULT": "",
}
"""The required grub options for OTA grub boot control.

For entry name starts with `-`, it means the entry must be REMOVED.
"""


OTA_MANAGED_CFG_HEADER = """\
# OTAClient managed configuration file, DO NOT EDIT!
#
# Manual edits to this file will NOT be preserved across OTA!
"""


@dataclass
class OTAManagedCfg:
    """
    Example:

    ```
    # ------ OTA managed metadata ------ #
    # OTAClient_version: vx.xx.x
    # grub_version: x.xx
    # checksum: sha256:xxxxxxx
    # ------ End of OTA managed metadata ------ #
    ```
    """

    FOOTER_HEAD: ClassVar[str] = "# ------ OTA managed metadata ------ #"
    FOOTER_TAIL: ClassVar[str] = "# ------ End of OTA managed metadata ------ #"

    raw_contents: str

    grub_version: str
    checksum: str
    otaclient_version: str = version


class OTASlotSuffix(StrEnum):
    slot_a = "_a"
    slot_b = "_b"


class OTASlotBootID(StrEnum):
    slot_a = f"{grub_new_cfg.OTA_BOOT_SLOT}_a"
    slot_b = f"{grub_new_cfg.OTA_BOOT_SLOT}_b"

    def get_suffix(self) -> str:
        return f"_{self.rsplit('_', 1)[-1]}"


@dataclass
class _ABPartition:
    boot_efi_partition_uuid: str
    boot_partition_uuid: str
    slot_a_uuid: str
    slot_b_uuid: str

    @classmethod
    def detect_ab_partition(cls) -> Self:
        return cls()


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

        The recovery entry for the same kernel will be filtered.
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
        """Fix up the found entry.

        1. fix up menuentry title and id to `slot_boot_id`.
        2. fix up the linux and initrd path to prefix `/boot/<slot_boot_id>`.
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
        """Generate a menuentry block for `slot_boot_id` with matching the given kernel version.

        This searches through all menuentry blocks in grub-mkconfig output
        and returns the first non-recovery entry whose linux kernel version
        matches <kernel_ver>.
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
