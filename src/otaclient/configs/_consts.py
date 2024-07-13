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
"""OTAClient internal use constants and fixed definitions."""


from __future__ import annotations

import logging
import warnings
from enum import Enum, auto
from typing import Any, Literal

logger = logging.getLogger(__name__)


class Consts:
    # ------ otaclient configuration files ------ #
    OTA_DIR = "/boot/ota"
    ECU_INFO_FNAME = "ecu_info.yaml"
    PROXY_INFO_FNAME = "proxy_info.yaml"

    # ------ ota status file ------ #
    OTA_STATUS_FNAME = "status"
    FIRMWARE_VERSION_FNAME = "version"
    SLOT_IN_USE_FNAME = "slot_in_use"

    # ------ compression support ------ #
    SUPPORTED_COMPRESS_ALG = ("zst", "zstd")


class CreateStandbyMechanism(str, Enum):
    """Create standby slot implementation."""

    DEFAULT = auto()
    LEGACY = auto()  # deprecated and removed
    REBUILD = auto()  # default
    IN_PLACE = auto()  # not yet implemented

    @classmethod
    def warning_validator(cls, value: Any) -> Literal[CreateStandbyMechanism.REBUILD]:
        """This validator is for pydantic use when parsing configuration file.

        Currently only REBUILD mode is supported.
        """
        if value != cls.REBUILD:
            _warning_msg = (
                "Currently only REBUILD mode is implemented, force set to REBUILD mode."
            )
            warnings.warn(_warning_msg, UserWarning, stacklevel=2)
            logger.warning(_warning_msg)
        return cls.REBUILD


class BootloaderType(str, Enum):
    """Bootloaders that supported by otaclient.

    auto_detect: (DEPRECATED) at runtime detecting what bootloader is in use in this ECU.
    grub: generic x86_64 platform with grub.
    cboot: ADLink rqx-580, rqx-58g, with BSP 32.5.x.
        (theoretically other Nvidia jetson xavier devices using cboot are also supported)
    rpi_boot: raspberry pi 4 with eeprom version newer than 2020-10-28(with tryboot support).
    """

    AUTO_DETECT = auto()
    GRUB = auto()
    CBOOT = auto()  # deprecated, use jetson-cboot instead
    JETSON_CBOOT = auto()
    RPI_BOOT = auto()

    @classmethod
    def deprecation_validator(cls, value: Any):
        """This validator is for pydantic use when parsing configuration file."""
        if value == BootloaderType.AUTO_DETECT:
            _warning_msg = (
                "bootloader type is not set or set to auto_detect(DEPRECATED), "
                "runtime bootloader type detection is UNRELIABLE and bootloader field "
                "SHOULD be set in ecu_info.yaml or set via environmental variables."
            )
            warnings.warn(_warning_msg, DeprecationWarning, stacklevel=2)
            logger.warning(_warning_msg)
        return value


class PathConsts:
    """Paths that are static and rooted at /."""

    # ------ otaclient runtime used dirs ------ #
    OTACLIENT_PID_FILE = "/run/otaclient.pid"
    OTACLIENT_RUN_DIR = "/run/otaclient"
    OTACLIENT_MOUNT_SPACE = "/run/otaclient/mnt"
    ACTIVE_SLOT_MOUNT = "/run/otaclient/mnt/active_slot"
    STANDY_SLOT_MOUNT = "/run/otaclient/mnt/standby_slot"

    OTA_IMAGE_META_FOLDER = "/opt/ota/image-meta"
    OTA_TMP_STORE = "/.ota-tmp"
    """OTA temporary storage at standby slot during OTA."""

    OTAPROXY_EXTERNAL_CACHE_STORAGE_MOUNT = "/run/otaclient/mnt/external_cache_src"
    OTAPROXY_EXTERNAL_CACHE_STORAGE_DATA_DIR = (
        f"{OTAPROXY_EXTERNAL_CACHE_STORAGE_MOUNT}/data"
    )

    # ------ common system paths ------ #
    ETC_DPATH = "/etc"
    BOOT_DIR = "/boot"

    # ------ otaclient installation ------ #
    OTACLIENT_INSTALLATION_DPATH = "/opt/ota/client"
    OTACLIENT_CERTS_DPATH = f"{OTACLIENT_INSTALLATION_DPATH}/certs"
    OTA_IMAGE_META_FOLDER = "/opt/ota/image-meta"

    # ------ otaclient configuration dir ------ #
    OTACLIENT_CONFIGS_DPATH = f"{BOOT_DIR}/ota"
    ECU_INFO_FPATH = f"{OTACLIENT_CONFIGS_DPATH}/{Consts.ECU_INFO_FNAME}"
    PROXY_INFO_FPATH = f"{OTACLIENT_CONFIGS_DPATH}/{Consts.PROXY_INFO_FNAME}"

    # ------ system files used/checked/updated by otaclient ------ #
    PASSWD_FPATH = f"{ETC_DPATH}/passwd"
    GROUP_FPATH = f"{ETC_DPATH}/passwd"
    FSTAB_FPATH = f"{ETC_DPATH}/fstab"
