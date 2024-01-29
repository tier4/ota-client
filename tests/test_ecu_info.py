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
import pytest
from pathlib import Path

from otaclient.configs.ecu_info import (
    DEFAULT_ECU_INFO,
    parse_ecu_info,
    ECUInfo,
    ECUContact,
    BootloaderType,
)


@pytest.mark.parametrize(
    "ecu_info_yaml, expected_ecu_info",
    (
        # --- case 1: invalid ecu_info --- #
        # case 1.1: valid yaml(empty file), invalid ecu_info
        (
            "# this is an empty file",
            DEFAULT_ECU_INFO,
        ),
        # case 1.2: valid yaml(array), invalid ecu_info
        (
            ("- this is an\n" "- yaml file that\n" "- contains a array\n"),
            DEFAULT_ECU_INFO,
        ),
        # case 1.2: invalid yaml
        (
            "    - \n not a \n [ valid yaml",
            DEFAULT_ECU_INFO,
        ),
        # --- case 2: single ECU --- #
        # case 2.1: basic single ECU
        (
            ("format_version: 1\n" 'ecu_id: "autoware"\n' 'ip_addr: "192.168.1.1"\n'),
            ECUInfo(
                ecu_id="autoware",
                ip_addr="192.168.1.1",
                bootloader=BootloaderType.UNSPECIFIED,
            ),
        ),
        # case 2.2: single ECU with bootloader type specified
        (
            (
                "format_version: 1\n"
                'ecu_id: "autoware"\n'
                'ip_addr: "192.168.1.1"\n'
                'bootloader: "grub"\n'
            ),
            ECUInfo(
                ecu_id="autoware",
                ip_addr="192.168.1.1",
                bootloader=BootloaderType.GRUB,
            ),
        ),
        # --- case 3: multiple ECUs --- #
        # case 3.1: basic multiple ECUs
        (
            (
                "format_version: 1\n"
                'ecu_id: "autoware"\n'
                'ip_addr: "192.168.1.1"\n'
                'available_ecu_ids: ["autoware", "p1", "p2"]\n'
                "secondaries: \n"
                '- ecu_id: "p1"\n'
                '  ip_addr: "192.168.0.11"\n'
                '- ecu_id: "p2"\n'
                '  ip_addr: "192.168.0.12"\n'
            ),
            ECUInfo(
                ecu_id="autoware",
                ip_addr="192.168.1.1",
                bootloader=BootloaderType.UNSPECIFIED,
                available_ecu_ids=["autoware", "p1", "p2"],
                secondaries=[
                    ECUContact(
                        ecu_id="p1",
                        ip_addr="192.168.0.11",
                    ),
                    ECUContact(
                        ecu_id="p2",
                        ip_addr="192.168.0.12",
                    ),
                ],
            ),
        ),
        # case 3.2: multiple ECUs, with main ECU's bootloader specified
        (
            (
                "format_version: 1\n"
                'ecu_id: "autoware"\n'
                'ip_addr: "192.168.1.1"\n'
                'bootloader: "grub"\n'
                'available_ecu_ids: ["autoware", "p1", "p2"]\n'
                "secondaries: \n"
                '- ecu_id: "p1"\n'
                '  ip_addr: "192.168.0.11"\n'
                '- ecu_id: "p2"\n'
                '  ip_addr: "192.168.0.12"\n'
            ),
            ECUInfo(
                ecu_id="autoware",
                ip_addr="192.168.1.1",
                available_ecu_ids=["autoware", "p1", "p2"],
                bootloader=BootloaderType.GRUB,
                secondaries=[
                    ECUContact(
                        ecu_id="p1",
                        ip_addr="192.168.0.11",
                    ),
                    ECUContact(
                        ecu_id="p2",
                        ip_addr="192.168.0.12",
                    ),
                ],
            ),
        ),
    ),
)
def test_ecu_info(tmp_path: Path, ecu_info_yaml: str, expected_ecu_info: ECUInfo):
    # --- preparation --- #
    (ota_dir := tmp_path / "boot" / "ota").mkdir(parents=True, exist_ok=True)
    (ecu_info_file := ota_dir / "ecu_info.yaml").write_text(ecu_info_yaml)

    # --- execution --- #
    loaded_ecu_info = parse_ecu_info(ecu_info_file)

    # --- assertion --- #
    assert loaded_ecu_info == expected_ecu_info
