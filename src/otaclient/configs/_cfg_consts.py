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
from typing import Any

from otaclient_common import replace_root

CANONICAL_ROOT = "/"


class CreateStandbyMechanism(str, Enum):
    LEGACY = "LEGACY"  # deprecated and removed
    REBUILD = "REBUILD"  # default
    IN_PLACE = "IN_PLACE"  # not yet implemented


_dynamic_loaded_paths = set(
    [
        "OPT_OTA_DPATH",
        "OTACLIENT_INSTALLATION",
        "CERT_DPATH",
        "IMAGE_META_DPATH",
        "BOOT_DPATH",
        "OTA_DPATH",
        "ECU_INFO_FPATH",
        "PROXY_INFO_FPATH",
        "ETC_DPATH",
        "PASSWD_FPATH",
        "GROUP_FPATH",
        "FSTAB_FPATH",
    ]
)


class Consts:

    @property
    def ACTIVE_ROOT(self) -> str:  # NOSONAR
        return self._ACTIVE_ROOT

    #
    # ------ fixed paths ------ #
    #
    """Paths that is fixed with dynamic root."""

    RUN_DIR = "/run/otaclient"
    OTACLIENT_PID_FILE = "/run/otaclient.pid"

    # runtime folder for holding ota related files
    RUNTIME_OTA_SESSION = "/run/otaclient/ota"

    MOUNT_SPACE = "/run/otaclient/mnt"
    ACTIVE_SLOT_MNT = "/run/otaclient/mnt/active_slot"
    STANDBY_SLOT_MNT = "/run/otaclient/mnt/standby_slot"

    OTA_TMP_STORE = "/.ota-tmp"
    """tmp store for local copy, located at standby slot."""

    #
    # ------ dynamic paths ------ #
    #
    OPT_OTA_DPATH = "/opt/ota"
    OTACLIENT_INSTALLATION = "/opt/ota/client"
    CERT_DPATH = "/opt/ota/client/certs"
    IMAGE_META_DPATH = "/opt/ota/image-meta"

    BOOT_DPATH = "/boot"
    OTA_DPATH = "/boot/ota"
    ECU_INFO_FPATH = "/boot/ota/ecu_info.yaml"
    PROXY_INFO_FPATH = "/boot/ota/proxy_info.yaml"

    ETC_DPATH = "/etc"
    PASSWD_FPATH = "/etc/passwd"
    GROUP_FPATH = "/etc/group"
    FSTAB_FPATH = "/etc/fstab"

    #
    # ------ consts ------ #
    #
    # ota status files
    OTA_STATUS_FNAME = "status"
    OTA_VERSION_FNAME = "version"
    SLOT_IN_USE_FNAME = "slot_in_use"

    OTA_API_SERVER_PORT = 50051
    OTAPROXY_LISTEN_PORT = 8082

    def __getattribute__(self, name: str) -> Any:
        try:
            attr = object.__getattribute__(self, name)
        except KeyError:
            raise AttributeError(f"{name} not found in {__name__}") from None

        if name == "ACTIVE_ROOT" or name not in _dynamic_loaded_paths:
            return attr
        return replace_root(attr, CANONICAL_ROOT, self.ACTIVE_ROOT)

    def __init__(self) -> None:
        """For future updating the ACTIVE_ROOT."""

        self._ACTIVE_ROOT = CANONICAL_ROOT


cfg_consts = Consts()
