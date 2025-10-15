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
"""otaclient internal uses consts, should not be changed from external."""

from __future__ import annotations

from otaclient_common import replace_root
from otaclient_common._typing import StrEnum

CANONICAL_ROOT = "/"


class CreateStandbyMechanism(StrEnum):
    LEGACY = "LEGACY"  # deprecated and removed
    REBUILD = "REBUILD"  # default
    IN_PLACE = "IN_PLACE"  # not yet implemented


class Consts:
    CANONICAL_ROOT = CANONICAL_ROOT

    @property
    def ACTIVE_ROOT(self) -> str:  # NOSONAR
        return self._ACTIVE_ROOT

    #
    # ------ paths ------ #
    #
    RUN_DIR = "/run/otaclient"
    OTACLIENT_PID_FILE = "/run/otaclient.pid"
    # runtime folder for holding ota related files
    RUNTIME_OTA_SESSION = "/run/otaclient/update-session"

    MOUNT_SPACE = "/run/otaclient/mnt"
    ACTIVE_SLOT_MNT = "/run/otaclient/mnt/active_slot"
    STANDBY_SLOT_MNT = "/run/otaclient/mnt/standby_slot"

    # mount point for downloaded otaclient
    DYNAMIC_CLIENT_MNT = "/run/otaclient/mnt/dynamic_otaclient"
    # mount point for host(original) root in dynamic client
    DYNAMIC_CLIENT_MNT_HOST_ROOT = "/host_root"
    # downloaded squashfs location in local filesystem
    # this path should not be included in the mounted rootfs
    DYNAMIC_CLIENT_SQUASHFS_FILE = "/.otaclient.squashfs"

    OTA_TMP_STORE = "/.ota-tmp"
    """For backward-compatibility"""
    OTA_RESOURCES_STORE = "/.ota-resources"
    OTA_META_STORE = "/.ota-meta"
    OTA_META_STORE_BASE_FILE_TABLE = "/.ota-meta/base"
    OTA_DOWNLOAD_DIR = "/.ota-download"

    OPT_OTA_DPATH = "/opt/ota"
    OTACLIENT_INSTALLATION = "/opt/ota/client"
    # squashfs location in image
    OTACLIENT_INSTALLATION_RELEASE = "/opt/ota/otaclient_release"
    CERT_DPATH = "/opt/ota/client/certs"
    IMAGE_META_DPATH = "/opt/ota/image-meta"

    BOOT_DPATH = "/boot"
    OTA_DPATH = "/boot/ota"
    ECU_INFO_FPATH = "/boot/ota/ecu_info.yaml"
    PROXY_INFO_FPATH = "/boot/ota/proxy_info.yaml"

    FSTAB_FPATH = "/etc/fstab"

    #
    # ------ consts ------ #
    #
    # ota status files
    OTA_STATUS_FNAME = "status"
    OTA_VERSION_FNAME = "version"
    SLOT_IN_USE_FNAME = "slot_in_use"

    DEFAULT_VERSION_STR = ""

    OTA_API_SERVER_PORT = 50051
    OTAPROXY_LISTEN_PORT = 8082

    #
    # ------ env vars ------ #
    #
    PREPARING_DOWNLOADED_DYNAMIC_OTA_CLIENT = "PREPARING_DOWNLOADED_DYNAMIC_OTA_CLIENT"
    RUNNING_DOWNLOADED_DYNAMIC_OTA_CLIENT = "RUNNING_DOWNLOADED_DYNAMIC_OTA_CLIENT"

    def __init__(self) -> None:
        """For future updating the ACTIVE_ROOT."""

        # TODO: detect rootfs here
        self._ACTIVE_ROOT = CANONICAL_ROOT


cfg_consts = Consts()


def dynamic_root(canonical_path: str) -> str:
    """Re-root the input path with the actual ACTIVE_ROOT."""
    if cfg_consts.ACTIVE_ROOT == CANONICAL_ROOT:
        return canonical_path
    return replace_root(canonical_path, CANONICAL_ROOT, cfg_consts.ACTIVE_ROOT)
