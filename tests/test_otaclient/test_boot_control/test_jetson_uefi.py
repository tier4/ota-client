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
from pytest_mock import MockerFixture

from otaclient.boot_control import _jetson_uefi
from otaclient.boot_control._jetson_common import BSPVersion
from otaclient.boot_control._jetson_uefi import (
    JetsonUEFIBootControlError,
    NVBootctrlJetsonUEFI,
    _detect_esp_dev,
)

MODULE = _jetson_uefi.__name__


class TestNVBootctrlJetsonUEFI:

    @pytest.mark.parametrize(
        "_input, expected",
        (
            (
                """\
Current version: 35.4.1
Capsule update status: 1
Current bootloader slot: A
Active bootloader slot: A
num_slots: 2
slot: 0,             status: normal
slot: 1,             status: normal
             """,
                BSPVersion.parse("35.4.1"),
            ),
            (
                """\
Current version: 36.3.0
Capsule update status: 0
Current bootloader slot: A
Active bootloader slot: A
num_slots: 2
slot: 0,             status: normal
slot: 1,             status: normal
             """,
                BSPVersion.parse("36.3.0"),
            ),
            (
                """\
Current version: 0.0.0
Current bootloader slot: A
Active bootloader slot: A
num_slots: 2
slot: 0,             status: normal
slot: 1,             status: normal
             """,
                ValueError(),
            ),
        ),
    )
    def get_current_fw_bsp_version(
        self, _input: str, expected: BSPVersion | Exception, mocker: MockerFixture
    ):
        mocker.patch.object(
            NVBootctrlJetsonUEFI,
            "dump_slots_info",
            mocker.MagicMock(return_value=_input),
        )

        if isinstance(expected, Exception):
            with pytest.raises(type(expected)):
                NVBootctrlJetsonUEFI.get_current_fw_bsp_version()
        else:
            assert NVBootctrlJetsonUEFI.get_current_fw_bsp_version() == expected


@pytest.mark.parametrize(
    "all_esp_devs, boot_parent_devpath, expected",
    (
        (
            ["/dev/mmcblk0p41", "/dev/nvme0n1p41"],
            "/dev/nvme0n1",
            "/dev/nvme0n1p41",
        ),
        (
            ["/dev/mmcblk0p41"],
            "/dev/mmcblk0",
            "/dev/mmcblk0p41",
        ),
        (
            ["/dev/sda23", "/dev/mmcblk0p41"],
            "/dev/nvme0n1",
            JetsonUEFIBootControlError(),
        ),
        (
            [],
            "/dev/nvme0n1",
            JetsonUEFIBootControlError(),
        ),
    ),
)
def test__detect_esp_dev(
    all_esp_devs, boot_parent_devpath, expected, mocker: MockerFixture
):
    mocker.patch(
        f"{MODULE}.CMDHelperFuncs.get_dev_by_token",
        mocker.MagicMock(return_value=all_esp_devs),
    )

    if isinstance(expected, Exception):
        with pytest.raises(type(expected)):
            _detect_esp_dev(boot_parent_devpath)
    else:
        assert _detect_esp_dev(boot_parent_devpath) == expected


class TestDetectDeviceOTABOOTDEV:

    pass
