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


from enum import Enum, auto
from logging import INFO
from pathlib import Path
from typing import Dict, Tuple

from otaclient import __file__ as _otaclient__init__

_OTACLIENT_PACKAGE_ROOT = Path(_otaclient__init__).parent

# NOTE: VERSION file is installed under otaclient package root
EXTRA_VERSION_FILE = str(_OTACLIENT_PACKAGE_ROOT / "version.txt")
OTACLIENT_LOCK_FILE = "/var/run/otaclient.lock"


class CreateStandbyMechanism(Enum):
    LEGACY = 0
    REBUILD = auto()
    IN_PLACE = auto()


class OtaClientServerConfig:
    SERVER_PORT = 50051
    WAITING_SUBECU_ACK_UPDATE_REQ_TIMEOUT = 6
    QUERYING_SUBECU_STATUS_TIMEOUT = 30
    LOOP_QUERYING_SUBECU_STATUS_INTERVAL = 10
    STATUS_UPDATE_INTERVAL = 1

    # proxy server
    OTA_PROXY_LISTEN_ADDRESS = "0.0.0.0"
    OTA_PROXY_LISTEN_PORT = 8082


class BaseConfig:
    """Platform neutral configuration."""

    # ------ otaclient logging setting ------ #
    DEFAULT_LOG_LEVEL = INFO
    LOG_LEVEL_TABLE: Dict[str, int] = {
        "otaclient.app.boot_control.cboot": INFO,
        "otaclient.app.boot_control.grub": INFO,
        "otaclient.app.ota_client": INFO,
        "otaclient.app.ota_client_service": INFO,
        "otaclient.app.ota_client_stub": INFO,
        "otaclient.app.ota_metadata": INFO,
        "otaclient.app.downloader": INFO,
        "otaclient.app.main": INFO,
    }
    LOG_FORMAT = (
        "[%(asctime)s][%(levelname)s]-%(filename)s:%(funcName)s:%(lineno)d,%(message)s"
    )

    # ------ common settings ------ #
    # NOTE: typically the common settings should not be changed!
    #       otherwise the backward compatibility will be impact.

    # --- common paths --- #
    # NOTE: certs dir is located at the otaclient package root
    CERTS_DIR = str(_OTACLIENT_PACKAGE_ROOT / "certs")
    ACTIVE_ROOTFS_PATH = "/"
    BOOT_DIR = "/boot"
    OTA_DIR = "/boot/ota"
    ECU_INFO_FILE = "/boot/ota/ecu_info.yaml"
    PROXY_INFO_FILE = "/boot/ota/proxy_info.yaml"
    PASSWD_FILE = "/etc/passwd"
    GROUP_FILE = "/etc/group"
    FSTAB_FPATH = "/etc/fstab"
    # where the OTA image meta store for this slot
    META_FOLDER = "/opt/ota/image-meta"

    # --- device configuration files --- #
    # this files should be placed under /boot/ota folder
    ECU_INFO_FNAME = "ecu_info.yaml"
    PROXY_INFO_FNAME = "proxy_info.yaml"

    # --- ota-status files --- #
    # this files should be placed under /boot/ota-status folder
    OTA_STATUS_FNAME = "status"
    OTA_VERSION_FNAME = "version"
    SLOT_IN_USE_FNAME = "slot_in_use"

    # --- otaclient internal used path --- #
    # standby/refroot mount points
    MOUNT_POINT = "/mnt/standby"
    # where active(old) image partition will be bind mounted to
    REF_ROOT_MOUNT_POINT = "/mnt/refroot"
    # tmp store for local copy
    OTA_TMP_STORE = "/.ota-tmp"
    # tmp store for standby slot OTA image meta
    OTA_TMP_META_STORE = "/.ota-meta"

    # ------ otaclient behavior setting ------ #
    CHUNK_SIZE = 1 * 1024 * 1024  # 1MB
    LOCAL_CHUNK_SIZE = 4 * 1024 * 1024  # 4MB
    DOWNLOAD_RETRY = 10
    DOWNLOAD_BACKOFF_MAX = 3  # seconds
    MAX_CONCURRENT_TASKS = 128
    MAX_DOWNLOAD_THREAD = 3
    DOWNLOADER_CONNPOOL_SIZE_PER_THREAD = 8
    STATS_COLLECT_INTERVAL = 1  # second
    ## standby creation mode, now only REBUILD mode is available
    STANDBY_CREATION_MODE = CreateStandbyMechanism.REBUILD
    # compressed OTA image support
    SUPPORTED_COMPRESS_ALG: Tuple[str, ...] = ("zst", "zstd")


# init cfgs
server_cfg = OtaClientServerConfig()
config = BaseConfig()
