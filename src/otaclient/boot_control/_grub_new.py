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

from dataclasses import dataclass
from typing import ClassVar

from typing_extensions import Self

from _otaclient_version import version
from otaclient_common._typing import StrEnum

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
class OTAManagedCfgFooter:
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

    grub_version: str
    checksum: str
    otaclient_version: str = version


class OTASlotSuffix(StrEnum):
    slot_a = "_a"
    slot_b = "_b"


@dataclass
class _ABPartition:
    boot_efi_partition_uuid: str
    boot_partition_uuid: str
    slot_a_uuid: str
    slot_b_uuid: str

    @classmethod
    def detect_ab_partition(cls) -> Self:
        return cls()
