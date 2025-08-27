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

import os
import stat
from pathlib import Path

import pytest

from ota_metadata.file_table.utils import (
    _apply_permissions,
    _apply_symlink_permissions,
)


class TestApplyPermissions:
    """Integration tests for permission functions with real files."""

    @pytest.mark.skipif(
        os.getuid() != 0, reason="Need root privileges for ownership changes"
    )
    def test_apply_permissions_real_file_as_root(self, tmp_path: Path):
        """Integration test for _apply_permissions with real file operations (requires root)."""
        test_file = tmp_path / "test_file.txt"
        test_file.touch()

        # Test changing to a different user/group and mode
        uid, gid, mode = 1000, 1000, 0o755
        _apply_permissions(test_file, uid=uid, gid=gid, mode=mode)

        # Verify the changes
        stat_result = test_file.lstat()
        assert stat_result.st_uid == uid
        assert stat_result.st_gid == gid
        # Use stat.S_IMODE to get only the permission bits
        assert stat.S_IMODE(stat_result.st_mode) == stat.S_IMODE(mode)

    @pytest.mark.skipif(
        os.getuid() != 0, reason="Need root privileges for ownership changes"
    )
    def test_apply_symlink_permissions_real_symlink_as_root(self, tmp_path: Path):
        """Integration test for _apply_symlink_permissions with real symlink operations (requires root)."""
        target_file = tmp_path / "target.txt"
        target_file.touch()
        symlink_file = tmp_path / "symlink.txt"
        symlink_file.symlink_to(target_file)

        # Test changing symlink ownership
        uid, gid = 1000, 1000
        _apply_symlink_permissions(symlink_file, uid=uid, gid=gid)

        # Verify the changes (use lstat to get symlink stats, not target stats)
        stat_result = symlink_file.lstat()
        assert stat_result.st_uid == uid
        assert stat_result.st_gid == gid

    def test_apply_permissions_mode_only_non_root(self, tmp_path: Path):
        """Integration test for _apply_permissions mode changes (works without root)."""
        test_file = tmp_path / "test_file.txt"
        test_file.touch()

        # Get current ownership
        current_stat = test_file.lstat()
        uid, gid = current_stat.st_uid, current_stat.st_gid

        # Only change mode
        new_mode = 0o755
        _apply_permissions(test_file, uid=uid, gid=gid, mode=new_mode)

        # Verify mode change
        stat_result = test_file.lstat()
        # Use stat.S_IMODE to get only the permission bits
        assert stat.S_IMODE(stat_result.st_mode) == stat.S_IMODE(new_mode)
        assert stat_result.st_uid == uid  # ownership unchanged
        assert stat_result.st_gid == gid  # ownership unchanged
