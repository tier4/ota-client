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
from typing import ClassVar

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


@dataclass
class _BootMenuEntry:
    MENUENTRY_PA: ClassVar[re.Pattern] = re.compile(
        r"^\s*menuentry[^\{]+\{[^\}]*\}",
        re.MULTILINE | re.DOTALL,
    )
    LINUX_PA_MULTILINE: ClassVar[re.Pattern] = re.compile(
        r"^\s*linux\s*(?P<linux_fpath>[^\s]+)", re.MULTILINE
    )
    LINUX_VERSION_PA: ClassVar[re.Pattern] = re.compile(r"vmlinuz-(?P<ver>[\.\w-]+)$")
    INITRD_PA_MULTILINE: ClassVar[re.Pattern] = re.compile(
        r"^\s*initrd\s*(?P<initrd_fpath>[^\s]+)", re.MULTILINE
    )
    MENUENTRY_TITLE_PA: ClassVar[re.Pattern] = re.compile(
        r"""^\s*menuentry\s+(?P<entry_title>(?:"[^"]*"|'[^']*'|[^\s]+))"""
    )
    MENUENTRY_ID_PA: ClassVar[re.Pattern] = re.compile(
        r"""^\s*menuentry.*?\$menuentry_id_option\s+(?P<entry_id>(?:"[^"]*"|'[^']*'|[^\s]+))"""
    )

    raw_entry: str
    slot_boot_id: OTASlotBootID
    kernel_ver: str

    @classmethod
    def find_menuentry(
        cls, _in: str, *, slot_boot_id: OTASlotBootID, kernel_ver: str
    ) -> Self:
        # NOTE: for specific kernel version, we should have exactly one
        #       boot entry(non-recovery entry) for it.
        _found: str
        for _found in cls.MENUENTRY_PA.findall(_in):
            _linux_dir_ma = cls.LINUX_PA_MULTILINE.search(_found)
            if not _linux_dir_ma:
                continue  # not a linux boot menuentry

            _linux_dir = _linux_dir_ma.group()
            if _linux_dir.find("recovery") >= 0:
                continue  # skip recovery entry

            _linux_ver_ma = cls.LINUX_VERSION_PA.search(_linux_dir)
            if not _linux_ver_ma:
                continue  # invalid linux entry

            if _linux_ver_ma.group("ver") != kernel_ver:
                continue  # not the entry we are looking for
            break  # found one
        else:
            raise ValueError("failed to find any matching entry from the input")

        #
        # ------ fix up the found entry ------ #
        #
        # fix up the menuentry title
        _found = cls.MENUENTRY_TITLE_PA.sub(
            lambda ma: ma.group().replace(ma.group("entry_title"), slot_boot_id, 1),
            _found,
        )
        # fix up the menuentry id
        _found = cls.MENUENTRY_ID_PA.sub(
            lambda ma: ma.group().replace(ma.group("entry_id"), slot_boot_id, 1),
            _found,
        )
        # fix up vmlinuz and initrd fpath
        _found = cls.LINUX_PA_MULTILINE.sub(
            lambda ma: ma.group().replace(
                ma.group("linux_fpath"),
                f"/{slot_boot_id}{ma.group('linux_fpath')}",
                1,
            ),
            _found,
        )
        _found = cls.INITRD_PA_MULTILINE.sub(
            lambda ma: ma.group().replace(
                ma.group("initrd_fpath"),
                f"/{slot_boot_id}{ma.group('initrd_fpath')}",
                1,
            ),
            _found,
        )

        return cls(raw_entry=_found, slot_boot_id=slot_boot_id, kernel_ver=kernel_ver)
