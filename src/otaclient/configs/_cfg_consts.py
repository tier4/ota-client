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
"""otaclient internal uses consts."""


from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

from otaclient_common import replace_root

ACTIVE_ROOT = "/"
"""Will be dynmaically detected in the future."""


class CreateStandbyMechanism(str, Enum):
    LEGACY = "LEGACY"  # deprecated and removed
    REBUILD = "REBUILD"  # default
    IN_PLACE = "IN_PLACE"  # not yet implemented


class _FixedPaths:
    """Paths that is fixed with dynamic root."""

    RUN_DIR = "/run/otaclient"
    OTACLIENT_PID_FILE = "/run/otaclient.pid"
    CANONICAL_ROOT = "/"

    # runtime folder for holding ota related files
    RUNTIME_OTA_SESSION = f"{RUN_DIR}/ota"

    MOUNT_SPACE = f"{RUN_DIR}/mnt"
    ACTIVE_SLOT_MNT = f"{MOUNT_SPACE}/active_slot"
    STANDBY_SLOT_MNT = f"{MOUNT_SPACE}/standby_slot"

    OTA_TMP_STORE = "/.ota-tmp"
    """tmp store for local copy, located at standby slot."""


class _DynamicPaths:
    """Paths that dynamically rooted."""

    if not TYPE_CHECKING:

        def __getattribute__(self, name: str) -> Any:
            attr = super().__getattribute__(name)
            if name.startswith("_") or not isinstance(attr, str):
                return attr
            return replace_root(attr, _FixedPaths.CANONICAL_ROOT, ACTIVE_ROOT)

    OPT_OTA_DPATH = "/opt/ota"
    OTACLIENT_INSTALLATION = f"{OPT_OTA_DPATH}/client"
    CERT_DPATH = f"{OTACLIENT_INSTALLATION}/certs"
    IMAGE_META_DPATH = f"{OPT_OTA_DPATH}/image-meta"

    BOOT_DPATH = "/boot"
    OTA_DPATH = "/boot/ota"

    ETC_DPATH = "/etc"
    PASSWD_FPATH = "/etc/passwd"
    GROUP_FPATH = "/etc/group"
    FSTAB_FPATH = "/etc/fstab"


_dynamic_paths = _DynamicPaths()


class _Consts:
    # otaclient configuration files
    ECU_INFO_FNAME = "ecu_info.yaml"
    PROXY_INFO_FNAME = "proxy_info.yaml"

    # ota status files
    OTA_STATUS_FNAME = "status"
    OTA_VERSION_FNAME = "version"
    SLOT_IN_USE_FNAME = "slot_in_use"


# use the whole cfg_consts module like a single class
def __getattr__(name: str) -> Any:
    try:
        return globals()[name]
    except KeyError:
        pass

    for _target in [_FixedPaths, _dynamic_paths, _Consts]:
        try:
            return getattr(_target, name)
        except AttributeError:
            continue
    raise AttributeError(f"{name} not found in {__name__}")
