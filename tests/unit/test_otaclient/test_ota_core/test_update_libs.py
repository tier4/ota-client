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

from pathlib import Path

from pytest_mock import MockerFixture

from otaclient.ota_core._update_libs import process_persistents

MODULE = "otaclient.ota_core._update_libs."


def test_process_persistents_routes_to_overlay_upper(mocker: MockerFixture):
    """OTA overlay layout: files go to the standby rw_overlay upper, but uid/gid
    mapping still uses the standby rootfs passwd/group."""
    m_handler = mocker.patch(f"{MODULE}PersistFilesHandler")
    active = Path("/mnt/active")
    standby = Path("/mnt/standby")
    upper = Path("/mnt/standby_overlay/upper")

    process_persistents(
        ["/etc/hostname"],
        active_slot_mp=active,
        standby_slot_mp=standby,
        standby_persist_root=upper,
    )

    kwargs = m_handler.call_args.kwargs
    # files are seeded into the overlay upper, not the RO standby rootfs
    assert kwargs["dst_root"] == upper
    # uid/gid mapping is against the standby rootfs (the new image), not the upper
    assert kwargs["dst_passwd_file"] == standby / "etc/passwd"
    assert kwargs["dst_group_file"] == standby / "etc/group"
    assert kwargs["src_root"] == active
    m_handler.return_value.preserve_persist_entry.assert_called_once_with(
        "/etc/hostname"
    )


def test_process_persistents_legacy_defaults_to_standby_rootfs(mocker: MockerFixture):
    """Legacy all-RW-root A/B: without an overlay upper, files land on the
    standby rootfs as before."""
    m_handler = mocker.patch(f"{MODULE}PersistFilesHandler")

    process_persistents(
        ["/etc/hostname"],
        active_slot_mp=Path("/mnt/active"),
        standby_slot_mp=Path("/mnt/standby"),
    )

    assert m_handler.call_args.kwargs["dst_root"] == Path("/mnt/standby")


def test_process_persistents_ignores_swapfile(mocker: MockerFixture):
    """Swapfile entries are skipped (swap cannot live on the overlay)."""
    m_handler = mocker.patch(f"{MODULE}PersistFilesHandler")

    process_persistents(
        ["/swapfile", "/swap.img", "/etc/hostname"],
        active_slot_mp=Path("/mnt/active"),
        standby_slot_mp=Path("/mnt/standby"),
    )

    preserved = [
        c.args[0] for c in m_handler.return_value.preserve_persist_entry.call_args_list
    ]
    assert preserved == ["/etc/hostname"]
