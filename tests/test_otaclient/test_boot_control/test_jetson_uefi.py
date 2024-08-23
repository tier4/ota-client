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
from typing_extensions import LiteralString

from otaclient.boot_control import _jetson_uefi
from otaclient.boot_control._jetson_common import BSPVersion
from otaclient.boot_control._jetson_uefi import (
    JetsonUEFIBootControlError,
    L4TLauncherBSPVersionControl,
    NVBootctrlJetsonUEFI,
    _detect_esp_dev,
    _detect_ota_bootdev_is_qspi,
    _l4tlauncher_version_control,
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


@pytest.mark.parametrize(
    "nvbootctrl_conf, expected",
    (
        (
            """\
TNSPEC 3701-500-0005-A.0-1-0-cti-orin-agx-agx201-00-
TEGRA_CHIPID 0x23
TEGRA_OTA_BOOT_DEVICE /dev/mtdblock0
TEGRA_OTA_GPT_DEVICE /dev/mtdblock0
""",
            True,
        ),
        (
            """\
TNSPEC 2888-400-0004-M.0-1-2-jetson-xavier-rqx580-
TEGRA_CHIPID 0x19
TEGRA_OTA_BOOT_DEVICE /dev/mmcblk0boot0
TEGRA_OTA_GPT_DEVICE /dev/mmcblk0boot1
""",
            False,
        ),
        ("", None),
    ),
)
def test__detect_ota_bootdev_is_qspi(nvbootctrl_conf, expected):
    assert _detect_ota_bootdev_is_qspi(nvbootctrl_conf) is expected


class TestL4TLauncherBSPVersionControl:

    @pytest.mark.parametrize(
        "_in, expected",
        _test_case := (
            (
                "R35.4.1:ac17457772666351154a5952e3b87851a6398da2afcf3a38bedfc0925760bb0e",
                L4TLauncherBSPVersionControl(
                    bsp_ver=BSPVersion(35, 4, 1),
                    sha256_digest="ac17457772666351154a5952e3b87851a6398da2afcf3a38bedfc0925760bb0e",
                ),
            ),
            (
                "R35.5.0:3928e0feb84e37db87dc6705a02eec95a6fca2dccd3467b61fa66ed8e1046b67",
                L4TLauncherBSPVersionControl(
                    bsp_ver=BSPVersion(35, 5, 0),
                    sha256_digest="3928e0feb84e37db87dc6705a02eec95a6fca2dccd3467b61fa66ed8e1046b67",
                ),
            ),
            (
                "R36.3.0:b14fa3623f4078d05573d9dcf2a0b46ea2ae07d6b75d9843f9da6ff24db13718",
                L4TLauncherBSPVersionControl(
                    bsp_ver=BSPVersion(36, 3, 0),
                    sha256_digest="b14fa3623f4078d05573d9dcf2a0b46ea2ae07d6b75d9843f9da6ff24db13718",
                ),
            ),
        ),
    )
    def test_parse(self, _in, expected):
        assert L4TLauncherBSPVersionControl.parse(_in) == expected

    @pytest.mark.parametrize(
        "to_be_dumped, expected",
        tuple((entry[1], entry[0]) for entry in _test_case),
    )
    def test_dump(self, to_be_dumped: L4TLauncherBSPVersionControl, expected: str):
        assert to_be_dumped.dump() == expected


@pytest.mark.parametrize(
    "ver_ctrl, l4tlauncher_digest, current_bsp_ver, expected_bsp_ver, expected_ver_ctrl",
    (
        # valid version file, hash matched
        (
            "R36.3.0:b14fa3623f4078d05573d9dcf2a0b46ea2ae07d6b75d9843f9da6ff24db13718",
            "b14fa3623f4078d05573d9dcf2a0b46ea2ae07d6b75d9843f9da6ff24db13718",
            BSPVersion(1, 2, 3),
            BSPVersion(36, 3, 0),
            "R36.3.0:b14fa3623f4078d05573d9dcf2a0b46ea2ae07d6b75d9843f9da6ff24db13718",
        ),
        # no version file, lookup table hit
        (
            "invalid_version_file",
            "b14fa3623f4078d05573d9dcf2a0b46ea2ae07d6b75d9843f9da6ff24db13718",
            BSPVersion(1, 2, 3),
            BSPVersion(36, 3, 0),
            "R36.3.0:b14fa3623f4078d05573d9dcf2a0b46ea2ae07d6b75d9843f9da6ff24db13718",
        ),
        # valid version file, hash mismatched, use slot BSP version
        (
            "R36.3.0:b14fa3623f4078d05573d9dcf2a0b46ea2ae07d6b75d9843f9da6ff24db13718",
            "hash_mismatched",
            BSPVersion(1, 2, 3),
            BSPVersion(1, 2, 3),
            "R1.2.3:hash_mismatched",
        ),
        # no version file, lookup table unhit
        (
            "invalid_version_file",
            "not_recorded_hash",
            BSPVersion(1, 2, 3),
            BSPVersion(1, 2, 3),
            "R1.2.3:not_recorded_hash",
        ),
    ),
)
def test__l4tlauncher_version_control(
    ver_ctrl,
    l4tlauncher_digest,
    current_bsp_ver,
    expected_bsp_ver,
    expected_ver_ctrl,
    tmp_path,
    mocker: MockerFixture,
):
    ver_control_f = tmp_path / "l4tlauncher_ver_control"
    ver_control_f.write_text(ver_ctrl)

    mocker.patch(
        f"{MODULE}.file_sha256", mocker.MagicMock(return_value=l4tlauncher_digest)
    )

    assert (
        _l4tlauncher_version_control(
            ver_control_f,
            "any",
            current_slot_bsp_ver=current_bsp_ver,
        )
        == expected_bsp_ver
    )
    # ensure the version control file is expected
    assert ver_control_f.read_text() == expected_ver_ctrl
