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


import yaml
import pytest
from pathlib import Path
from otaclient.app.boot_control import BootloaderType
from typing import Any, List, Dict


@pytest.mark.parametrize(
    "ecu_info_dict, secondary_ecus, ecu_id, ip_addr, available_ecu_ids, bootloader",
    (
        (
            None,
            [],
            "autoware",
            "127.0.0.1",
            ["autoware"],
            BootloaderType.UNSPECIFIED,
        ),
        (
            {"format_version": 1, "ecu_id": "autoware"},
            [],
            "autoware",
            "127.0.0.1",
            ["autoware"],
            BootloaderType.UNSPECIFIED,
        ),
        (
            {"format_version": 2, "ecu_id": "autoware", "ip_addr": "192.168.1.1"},
            [],
            "autoware",
            "192.168.1.1",
            ["autoware"],
            BootloaderType.UNSPECIFIED,
        ),
        (
            {
                "format_version": 1,
                "ecu_id": "autoware",
                "secondaries": [
                    {"ecu_id": "perception1", "ip_addr": "192.168.0.11"},
                    {"ecu_id": "perception2", "ip_addr": "192.168.0.12"},
                ],
                "available_ecu_ids": ["autoware", "perception1", "perception2"],
            },
            [
                {"ecu_id": "perception1", "ip_addr": "192.168.0.11"},
                {"ecu_id": "perception2", "ip_addr": "192.168.0.12"},
            ],
            "autoware",
            "127.0.0.1",
            ["autoware", "perception1", "perception2"],
            BootloaderType.UNSPECIFIED,
        ),
        (
            {
                "format_version": 1,
                "bootloader": "grub",
                "ecu_id": "autoware",
                "secondaries": [
                    {"ecu_id": "perception1", "ip_addr": "192.168.0.11"},
                    {"ecu_id": "perception2", "ip_addr": "192.168.0.12"},
                ],
                "available_ecu_ids": ["autoware", "perception1", "perception2"],
            },
            [
                {"ecu_id": "perception1", "ip_addr": "192.168.0.11"},
                {"ecu_id": "perception2", "ip_addr": "192.168.0.12"},
            ],
            "autoware",
            "127.0.0.1",
            ["autoware", "perception1", "perception2"],
            BootloaderType.GRUB,
        ),
    ),
)
def test_ecu_info(
    tmp_path: Path,
    ecu_info_dict: Dict[str, Any],
    secondary_ecus: List[Dict[str, Any]],
    ecu_id: str,
    ip_addr: str,
    available_ecu_ids: List[str],
    bootloader: BootloaderType,
):
    from otaclient.app.ecu_info import ECUInfo

    boot_dir = tmp_path / "boot"
    boot_dir.mkdir()
    (boot_dir / "ota").mkdir()
    ecu_info_file = boot_dir / "ota" / "ecu_info.yaml"
    if ecu_info_dict is not None:
        ecu_info_file.write_text(yaml.dump(ecu_info_dict))

    ecu_info = ECUInfo.parse_ecu_info(ecu_info_file)
    assert ecu_info.secondaries == secondary_ecus
    assert ecu_info.ecu_id == ecu_id
    assert ecu_info.ip_addr == ip_addr
    assert ecu_info.get_available_ecu_ids() == available_ecu_ids
    assert ecu_info.get_bootloader() == bootloader
