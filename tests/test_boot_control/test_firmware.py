# Copyright 2023 TIER IV, INC. All rights reserved.
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


import pytest
from pathlib import Path
from unittest.mock import MagicMock, call, ANY

FIRMWARE_YAML = """
version: 1
firmwares:
  - file: rce.zst
    compression: zstd
    partitions:
      - /dev/mmcblk0p21
      - /dev/mmcblk0p22
  - file: xusb.zst
    compression: zstd
    partitions:
      - /dev/mmcblk0p19
      - /dev/mmcblk0p20
"""


@pytest.mark.parametrize(
    "slot, in_out_files",
    (
        (0, (("rce.zst", "/dev/mmcblk0p21"), ("xusb.zst", "/dev/mmcblk0p19"))),
        (1, (("rce.zst", "/dev/mmcblk0p22"), ("xusb.zst", "/dev/mmcblk0p20"))),
    ),
)
def test_firmware(tmp_path, slot, in_out_files):
    from otaclient.app.boot_control.firmware import Firmware

    firmware_config = tmp_path / "firmware.yaml"
    firmware_config.write_text(FIRMWARE_YAML)

    fw = Firmware(firmware_config)
    fw._copy = MagicMock()
    fw.update(slot)

    calls = []

    for files in in_out_files:
        calls.append(call(tmp_path / files[0], files[1], ANY))
    fw._copy.assert_has_calls(calls)
