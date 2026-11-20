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
"""Where otaclient and the OTA platform keep their things, outside the slots' files.

The partition-based layout's boot control keeps its state and stages its payloads
here; a parameter set's slot must hide none of it. Kept apart from that boot control,
so that what refuses such a slot does not import it.
"""

from __future__ import annotations

import os
from pathlib import Path

OTA_STATUS_DNAME = "ota-status"
"""The status directories, one per slot, under a platform's state directory."""

STATE_DPATHS = {"grub": "/boot", "l4t": "/opt/data", "driveos": "/opt/data"}
"""Where status files that must survive the reboot go, per device side the DPI may
report (see `_partition_image.PLATFORMS`)."""

STAGING_DPATH = "/opt/data/otaclient"
"""Where a payload is staged until the DPI has consumed it, on every device side."""

DPI_PLATFORM_PATHS = (
    "/opt/data/appdata",
    "/opt/data/bulk",
    "/opt/data/esync",
    "/opt/data/ota-agent",
    "/opt/data/ota-cache",
    "/opt/data/ota-client",
    "/opt/data/ota-du",
    "/opt/data/ota-esync",
    "/opt/data/ota-health",
    "/usr/local/lib/rootfs-ua",
    "/usr/local/sbin",
)
"""What the DPI and the platform it installs keep outside the slots' own files: on
optdata, the persist stores (appdata, bulk), the eSync client's tree, the agent
delivered on its own, otaclient's proxy cache and downloaded client (which
/ota-cache and /.otaclient.squashfs link to), DRIVE Update's and the DPI's records
and the health check's reason; in the image, the DPI and its wrapper, with the
platform's boot scripts.

From tier4/ota-esync-agent: deploy/common/ota-optdata-firstboot (the trees on
optdata), deploy/install-agent.sh (the DPI, its wrapper and directories) and
deploy/*/install-platform.sh (the links into optdata, the boot scripts). The eSync
download and staged packages are under /opt/data/ota (`OTA_PLATFORM_PATHS`)."""

APPLY_MAY_BE_IN = ("/usr/local/sbin",)
"""Reserved for a slot (one there would hide the DPI's wrapper and the platform's boot
scripts), but where a base's own `apply` may live: an `apply` there runs the base's
program, as any other of the base's."""

# TODO(ota-image-libs>0.8.0): in its RESERVED_PATHS from the next release on
OTA_PLATFORM_PATHS = ("/opt/data/ota", "/etc/ota", "/usr/lib/ota")
"""What the image library reserves from its next release on: the OTA stores on
optdata (the eSync download and staged packages, the parameter set store), and the
OTA platform's configuration and programs in the image."""


def _resolve_on(host_root: str, path: str) -> str | None:
    """`path` with every symbolic link on its way followed as the host follows it
    (an absolute target is the host's, at `host_root`), None when that cannot be
    told (a loop, or a link that leaves the host's root)."""
    _root = os.path.realpath(host_root)
    _todo = [_p for _p in path.split("/") if _p]
    _done: list[str] = []
    for _ in range(64):
        if not _todo:
            return "/" + "/".join(_done)
        _part = _todo.pop(0)
        if _part == ".":
            continue
        if _part == "..":
            _done = _done[:-1]
            continue
        _at = os.path.join(_root, *_done, _part)
        if not os.path.islink(_at):
            _done.append(_part)
            continue
        _target = os.readlink(_at)
        if _target.startswith("/"):
            _done = []
        _todo = [_p for _p in _target.split("/") if _p] + _todo
    return None


def platform_reserved_paths(
    *, dpi_executable: str | None = None, host_root: str = "/"
) -> tuple[str, ...]:
    """What otaclient and the platform keep outside the slots' own files, which a
    parameter set's slot must not hide: otaclient's status directories (with the boot
    an update was staged in) and staging directory of every device side, the OTA
    platform's and the DPI's paths, and the DPI this ECU names (`dpi_executable`):
    as named, and the file it resolves to on the host at `host_root` with the
    directory that holds it."""
    _paths = {STAGING_DPATH, *DPI_PLATFORM_PATHS, *OTA_PLATFORM_PATHS}
    _paths.update(str(Path(_d) / OTA_STATUS_DNAME) for _d in STATE_DPATHS.values())
    if dpi_executable:
        _paths.add(os.path.normpath(dpi_executable))
        if (_real := _resolve_on(host_root, dpi_executable)) is not None:
            _paths.update((_real, os.path.dirname(_real)))
    # what holds everything is no one's to reserve
    return tuple(sorted(_p for _p in _paths if _p != "/"))
