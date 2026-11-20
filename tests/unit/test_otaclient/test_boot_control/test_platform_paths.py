# Copyright 2026 TIER IV, INC. All rights reserved.
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
"""What otaclient and the OTA platform keep outside the slots' own files."""

from __future__ import annotations

from pathlib import Path

from otaclient.boot_control._partition_image import PLATFORMS
from otaclient.boot_control._platform_paths import (
    OTA_STATUS_DNAME,
    platform_reserved_paths,
)


def test_the_partition_layouts_own_state_and_staging_are_reserved():
    """Taken from where the partition boot control keeps them, every device side."""
    _reserved = set(platform_reserved_paths())
    for _platform in PLATFORMS.values():
        assert _platform.staging_dir in _reserved
        assert str(Path(_platform.state_dir) / OTA_STATUS_DNAME) in _reserved
    assert {"/opt/data/appdata", "/opt/data/bulk", "/usr/local/sbin"} <= _reserved
    assert len(_reserved) == len(platform_reserved_paths()), "each once"


def test_the_dpi_is_resolved_as_the_host_resolves_it(tmp_path: Path):
    """An absolute link on the host is the host's, not this root's."""
    (tmp_path / "usr/local/lib/vendor").mkdir(parents=True)
    (tmp_path / "usr/local/lib/vendor/dpi").write_text("#!/bin/sh\n")
    (tmp_path / "usr/bin").mkdir(parents=True)
    (tmp_path / "usr/bin/dpi").symlink_to("/usr/local/lib/vendor/dpi")
    _reserved = platform_reserved_paths(
        dpi_executable="/usr/bin/dpi", host_root=str(tmp_path)
    )
    assert {
        "/usr/bin/dpi",
        "/usr/local/lib/vendor/dpi",
        "/usr/local/lib/vendor",
    } <= set(_reserved)
    assert "/usr/bin" not in _reserved, "only the DPI, not what holds its link"


def test_a_dpi_link_that_loops_is_reserved_as_named(tmp_path: Path):
    (tmp_path / "opt").mkdir()
    (tmp_path / "opt/a").symlink_to("/opt/b")
    (tmp_path / "opt/b").symlink_to("/opt/a")
    _reserved = platform_reserved_paths(
        dpi_executable="/opt/a", host_root=str(tmp_path)
    )
    assert "/opt/a" in _reserved
    assert "/opt" not in _reserved and "/" not in _reserved
