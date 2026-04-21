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

from subprocess import CalledProcessError

import pytest
from pytest_mock import MockerFixture

from otaclient.configs._cfg_consts import StorageDeviceType
from otaclient_common.cmdhelper import (
    _read_sysfs_rotational,
    detect_storage_device_type,
)

_CMDHELPER = "otaclient_common.cmdhelper"


class TestReadSysfsRotational:
    def test_rotational_returns_true_for_hdd(self, mocker: MockerFixture):
        mocker.patch("pathlib.Path.read_text", return_value="1\n")
        assert _read_sysfs_rotational("sda") is True

    def test_non_rotational_returns_false_for_ssd(self, mocker: MockerFixture):
        mocker.patch("pathlib.Path.read_text", return_value="0\n")
        assert _read_sysfs_rotational("sda") is False

    def test_oserror_returns_none(self, mocker: MockerFixture):
        mocker.patch("pathlib.Path.read_text", side_effect=OSError("No such file"))
        assert _read_sysfs_rotational("sda") is None


class TestDetectStorageDeviceType:
    @pytest.mark.parametrize(
        "parent_devpath, expected",
        [
            pytest.param("/dev/nvme0n1", "L1", id="nvme0n1"),
            pytest.param("/dev/nvme1n1", "L1", id="nvme1n1"),
        ],
    )
    def test_nvme_classified_as_l1(
        self, mocker: MockerFixture, parent_devpath: str, expected: str
    ):
        mocker.patch(f"{_CMDHELPER}.get_parent_dev", return_value=parent_devpath)
        assert detect_storage_device_type("/dev/nvme0n1p1") == expected

    @pytest.mark.parametrize(
        "parent_devpath, expected",
        [
            pytest.param("/dev/mmcblk0", "L3", id="mmcblk0"),
            pytest.param("/dev/mmcblk1", "L3", id="mmcblk1"),
        ],
    )
    def test_emmc_classified_as_l3(
        self, mocker: MockerFixture, parent_devpath: str, expected: str
    ):
        mocker.patch(f"{_CMDHELPER}.get_parent_dev", return_value=parent_devpath)
        assert detect_storage_device_type("/dev/mmcblk0p1") == expected

    def test_sata_ssd_classified_as_l2(self, mocker: MockerFixture):
        mocker.patch(f"{_CMDHELPER}.get_parent_dev", return_value="/dev/sda")
        mocker.patch(f"{_CMDHELPER}._read_sysfs_rotational", return_value=False)
        assert detect_storage_device_type("/dev/sda1") == "L2"

    def test_sata_hdd_classified_as_l3(self, mocker: MockerFixture):
        mocker.patch(f"{_CMDHELPER}.get_parent_dev", return_value="/dev/sda")
        mocker.patch(f"{_CMDHELPER}._read_sysfs_rotational", return_value=True)
        assert detect_storage_device_type("/dev/sda1") == "L3"

    def test_sdx_rotational_unreadable_falls_back_to_l3(self, mocker: MockerFixture):
        mocker.patch(f"{_CMDHELPER}.get_parent_dev", return_value="/dev/sdb")
        mocker.patch(f"{_CMDHELPER}._read_sysfs_rotational", return_value=None)
        assert detect_storage_device_type("/dev/sdb1") == "L3"

    def test_unknown_device_falls_back_to_l3(self, mocker: MockerFixture):
        mocker.patch(f"{_CMDHELPER}.get_parent_dev", return_value="/dev/vda")
        assert detect_storage_device_type("/dev/vda1") == "L3"

    def test_get_parent_dev_failure_falls_back_to_l3(self, mocker: MockerFixture):
        mocker.patch(
            f"{_CMDHELPER}.get_parent_dev",
            side_effect=CalledProcessError(1, "lsblk"),
        )
        assert detect_storage_device_type("/dev/sda1") == "L3"


class TestStorageDeviceTypeMapDownloadThreads:
    """Test thread count calculation: factor = (cpu_count or 4) + 4.

    L1: min(24, max(16, factor)) → range [16, 24]
    L2: min(20, max(16, factor)) → range [16, 20]
    L3: min(14, max(8, factor))  → range [8, 14]
    """

    @pytest.mark.parametrize(
        "device_type, cpu_count, expected",
        [
            # L1: range [16, 24]
            pytest.param(StorageDeviceType.L1, 4, 16, id="L1_4cpu_clamped_min"),
            pytest.param(StorageDeviceType.L1, 16, 20, id="L1_16cpu_mid_range"),
            pytest.param(StorageDeviceType.L1, 24, 24, id="L1_24cpu_clamped_max"),
            # L2: range [16, 20]
            pytest.param(StorageDeviceType.L2, 4, 16, id="L2_4cpu_clamped_min"),
            pytest.param(StorageDeviceType.L2, 12, 16, id="L2_12cpu_at_min"),
            pytest.param(StorageDeviceType.L2, 24, 20, id="L2_24cpu_clamped_max"),
            # L3: range [8, 14]
            pytest.param(StorageDeviceType.L3, 2, 8, id="L3_2cpu_clamped_min"),
            pytest.param(StorageDeviceType.L3, 6, 10, id="L3_6cpu_mid_range"),
            pytest.param(StorageDeviceType.L3, 16, 14, id="L3_16cpu_clamped_max"),
        ],
    )
    def test_thread_calculation(
        self,
        device_type: StorageDeviceType,
        cpu_count: int,
        expected: int,
    ):
        assert device_type.map_to_download_threads(cpu_count) == expected

    @pytest.mark.parametrize(
        "cpu_count",
        [pytest.param(0, id="zero"), pytest.param(None, id="none")],
    )
    def test_falsy_cpu_count_defaults_to_4(self, cpu_count):
        """When cpu_count is falsy (0 or None), it defaults to 4, so factor = 8."""
        assert StorageDeviceType.L3.map_to_download_threads(cpu_count) == 8
