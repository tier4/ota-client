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

from otaclient_common import cmdhelper

MODULE = cmdhelper.__name__


class TestMkfsExt4:
    def test_mkfs_ext4_with_fslabel_and_fsuuid(self, mocker: MockerFixture):
        mock_subprocess = mocker.patch(f"{MODULE}.subprocess_call")
        cmdhelper.mkfs_ext4("/dev/sda1", fslabel="test-label", fsuuid="test-uuid")
        expected_cmd = [
            "mkfs.ext4",
            "-F",
            "-U",
            "test-uuid",
            "-L",
            "test-label",
            "/dev/sda1",
        ]
        mock_subprocess.assert_called_once_with(expected_cmd, raise_exception=True)

    def test_mkfs_ext4_preserve_existing_attributes(self, mocker: MockerFixture):
        mock_subprocess = mocker.patch(f"{MODULE}.subprocess_call")
        mocker.patch(
            f"{MODULE}.get_attrs_by_dev",
            side_effect=["existing-uuid", "existing-label"],
        )
        cmdhelper.mkfs_ext4("/dev/sda1")
        expected_cmd = [
            "mkfs.ext4",
            "-F",
            "-U",
            "existing-uuid",
            "-L",
            "existing-label",
            "/dev/sda1",
        ]
        mock_subprocess.assert_called_once_with(expected_cmd, raise_exception=True)


class TestEnsureMount:
    def test_ensure_mount_with_custom_parameters(self, mocker: MockerFixture):
        """Test ensure_mount with custom retry parameters."""
        mock_mount_func = mocker.Mock()
        mocker.patch(f"{MODULE}.is_target_mounted", return_value=True)

        cmdhelper.ensure_mount(
            target="/dev/nvme0n1p1",
            mnt_point="/mnt/custom",
            mount_func=mock_mount_func,
            raise_exception=False,
            set_unbindable=False,
            max_retry=10,
            retry_interval=5,
        )

        mock_mount_func.assert_called_once_with(
            target="/dev/nvme0n1p1", mount_point="/mnt/custom", set_unbindable=False
        )


class TestEnsureUmount:
    @pytest.mark.parametrize(
        "is_mounted, expected_subprocess_called",
        (
            pytest.param(True, True, id="mounted_target_calls_umount"),
            pytest.param(False, False, id="unmounted_target_no_umount"),
        ),
    )
    def test_umount(
        self,
        is_mounted: bool,
        expected_subprocess_called: bool,
        mocker: MockerFixture,
    ):
        mocker.patch(f"{MODULE}.is_target_mounted", return_value=is_mounted)
        mock_subprocess = mocker.patch(f"{MODULE}.subprocess_call")
        cmdhelper.umount("/mnt")
        if expected_subprocess_called:
            mock_subprocess.assert_called_once()
        else:
            mock_subprocess.assert_not_called()


class TestEnsureMountpoint:
    def test_ensure_mountpoint_create_directory(self, mocker: MockerFixture):
        mock_exists = mocker.patch("pathlib.Path.exists", return_value=False)
        mock_mkdir = mocker.patch("pathlib.Path.mkdir")
        mock_umount = mocker.patch(f"{MODULE}.ensure_umount")

        cmdhelper.ensure_mount_point("/test/mount", ignore_error=False)

        mock_exists.assert_called()
        mock_mkdir.assert_called_once_with(exist_ok=True, parents=True)
        mock_umount.assert_not_called()

    def test_ensure_mountpoint_umount_existing(self, mocker: MockerFixture):
        mocker.patch("pathlib.Path.exists", return_value=True)
        mocker.patch("pathlib.Path.is_symlink", return_value=False)
        mocker.patch("pathlib.Path.is_dir", return_value=True)
        mock_umount = mocker.patch(f"{MODULE}.ensure_umount")

        cmdhelper.ensure_mount_point("/test/mount", ignore_error=False)

        mock_umount.assert_called_once()

    def test_ensure_mountpoint_remove_symlink(self, mocker: MockerFixture):
        mocker.patch("pathlib.Path.is_symlink", return_value=True)
        mock_unlink = mocker.patch("pathlib.Path.unlink")
        mocker.patch("pathlib.Path.exists", return_value=False)
        mock_mkdir = mocker.patch("pathlib.Path.mkdir")

        cmdhelper.ensure_mount_point("/test/mount", ignore_error=False)

        mock_unlink.assert_called_once_with(missing_ok=True)
        mock_mkdir.assert_called_once_with(exist_ok=True, parents=True)
