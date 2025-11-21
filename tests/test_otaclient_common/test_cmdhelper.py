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

from unittest import mock

from otaclient_common import cmdhelper


class TestMkfsExt4:
    def test_mkfs_ext4_with_fslabel_and_fsuuid(self):
        with mock.patch(
            "otaclient_common.cmdhelper.subprocess_call"
        ) as mock_subprocess:
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

    def test_mkfs_ext4_preserve_existing_attributes(self):
        with mock.patch(
            "otaclient_common.cmdhelper.subprocess_call"
        ) as mock_subprocess:
            with mock.patch(
                "otaclient_common.cmdhelper.get_attrs_by_dev"
            ) as mock_get_attrs:
                mock_get_attrs.side_effect = ["existing-uuid", "existing-label"]
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
                mock_subprocess.assert_called_once_with(
                    expected_cmd, raise_exception=True
                )


class TestEnsureMount:
    def test_ensure_mount_with_custom_parameters(self):
        """Test ensure_mount with custom retry parameters."""
        mock_mount_func = mock.Mock()

        with mock.patch(
            "otaclient_common.cmdhelper.is_target_mounted"
        ) as mock_is_mounted:
            mock_is_mounted.return_value = True

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
    def test_ensure_umount_mounted_target(self):
        with mock.patch(
            "otaclient_common.cmdhelper.is_target_mounted"
        ) as mock_is_mounted:
            with mock.patch(
                "otaclient_common.cmdhelper.subprocess_call"
            ) as mock_subprocess:
                mock_is_mounted.return_value = True
                cmdhelper.umount("/mnt")
                mock_subprocess.assert_called_once()

    def test_ensure_umount_not_mounted_target(self):
        with mock.patch(
            "otaclient_common.cmdhelper.is_target_mounted"
        ) as mock_is_mounted:
            with mock.patch(
                "otaclient_common.cmdhelper.subprocess_call"
            ) as mock_subprocess:
                mock_is_mounted.return_value = False
                cmdhelper.umount("/mnt")
                mock_subprocess.assert_not_called()


class TestEnsureMountpoint:
    def test_ensure_mountpoint_create_directory(self):
        with mock.patch("pathlib.Path.exists") as mock_exists:
            with mock.patch("pathlib.Path.mkdir") as mock_mkdir:
                with mock.patch(
                    "otaclient_common.cmdhelper.ensure_umount"
                ) as mock_umount:
                    mock_exists.return_value = False
                    cmdhelper.ensure_mount_point("/test/mount", ignore_error=False)
                    mock_mkdir.assert_called_once_with(exist_ok=True, parents=True)
                    mock_umount.assert_not_called()

    def test_ensure_mountpoint_umount_existing(self):
        with mock.patch("pathlib.Path.exists") as mock_exists:
            with mock.patch("pathlib.Path.is_symlink") as mock_is_symlink:
                with mock.patch("pathlib.Path.is_dir") as mock_is_dir:
                    with mock.patch(
                        "otaclient_common.cmdhelper.ensure_umount"
                    ) as mock_umount:
                        mock_exists.return_value = True
                        mock_is_symlink.return_value = False
                        mock_is_dir.return_value = True
                        cmdhelper.ensure_mount_point("/test/mount", ignore_error=False)
                        mock_umount.assert_called_once()

    def test_ensure_mountpoint_remove_symlink(self):
        with mock.patch("pathlib.Path.is_symlink") as mock_is_symlink:
            with mock.patch("pathlib.Path.unlink") as mock_unlink:
                with mock.patch("pathlib.Path.exists") as mock_exists:
                    with mock.patch("pathlib.Path.mkdir") as mock_mkdir:
                        mock_is_symlink.return_value = True
                        mock_exists.return_value = False
                        cmdhelper.ensure_mount_point("/test/mount", ignore_error=False)
                        mock_unlink.assert_called_once_with(missing_ok=True)
                        mock_mkdir.assert_called_once_with(exist_ok=True, parents=True)
