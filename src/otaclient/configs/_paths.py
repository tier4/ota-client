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
"""Path consts used by otaclient."""


from __future__ import annotations

from ._common import DynamicRootMixin, ExtractAttrsMixin
from ._consts import consts


class _StaticPathConsts(ExtractAttrsMixin):
    """Paths that are static and rooted at /."""

    OTACLIENT_PID_FILE = "/run/otaclient.pid"
    OTACLIENT_RUN_DIR = "/run/otaclient"
    OTACLIENT_MOUNT_SPACE = "/run/otaclient/mnt"
    OTACLIENT_INSTALLATION_DPATH = "/opt/ota/client"
    ACTIVE_SLOT_MOUNT = "/run/otaclient/mnt/active_slot"
    STANDY_SLOT_MOUNT = "/run/otaclient/mnt/standby_slot"

    OTA_IMAGE_META_FOLDER = "/opt/ota/image-meta"
    OTA_TMP_STORE = "/.ota-tmp"
    """OTA temporary storage at standby slot during OTA."""

    OTAPROXY_EXTERNAL_CACHE_STORAGE_MOUNT = "/run/otaclient/mnt/external_cache_src"
    OTAPROXY_EXTERNAL_CACHE_STORAGE_DATA_DIR = (
        f"{OTAPROXY_EXTERNAL_CACHE_STORAGE_MOUNT}/data"
    )


class _DynamicPathConsts(DynamicRootMixin, ExtractAttrsMixin):
    """Paths that will be dynamically re-rooted to <HOST_ROOTFS>."""

    # ------ common system paths ------ #
    ETC_DPATH = "/etc"
    BOOT_DIR = "/boot"

    # ------ otaclient installation ------ #
    OTACLIENT_INSTALLATION_DPATH = _StaticPathConsts.OTACLIENT_INSTALLATION_DPATH
    OTACLIENT_CERTS_DPATH = f"{OTACLIENT_INSTALLATION_DPATH}/certs"
    OTA_IMAGE_META_FOLDER = "/opt/ota/image-meta"

    # ------ otaclient configuration dir ------ #
    OTACLIENT_CONFIGS_DPATH = f"{BOOT_DIR}/ota"
    ECU_INFO_FPATH = f"{OTACLIENT_CONFIGS_DPATH}/{consts.ECU_INFO_FNAME}"
    PROXY_INFO_FPATH = f"{OTACLIENT_CONFIGS_DPATH}/{consts.PROXY_INFO_FNAME}"

    # ------ system files used/checked/updated by otaclient ------ #
    PASSWD_FPATH = f"{ETC_DPATH}/passwd"
    GROUP_FPATH = f"{ETC_DPATH}/passwd"
    FSTAB_FPATH = f"{ETC_DPATH}/fstab"
