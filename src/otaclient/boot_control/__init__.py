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


from pathlib import Path

from otaclient_common.common import copytree_identical

from .configs import BootloaderType
from .protocol import BootControllerProtocol
from .selecter import detect_bootloader, get_boot_controller

__all__ = (
    "get_boot_controller",
    "detect_bootloader",
    "BootloaderType",
    "BootControllerProtocol",
)


def preserve_ota_config_files_to_standby(
    *, active_slot_ota_dirpath: Path, standby_slot_ota_dirpath: Path
) -> None:
    """Preserve /boot/ota to standby /boot folder.

    Only called when standby slot doesn't have /boot/ota configured.
    """
    if not active_slot_ota_dirpath.is_dir():
        return
    copytree_identical(active_slot_ota_dirpath, standby_slot_ota_dirpath)
