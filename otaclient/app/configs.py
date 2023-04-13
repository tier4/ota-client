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


class CreateStandbyMechanism(Enum):
    LEGACY = 0  # deprecated and removed
    REBUILD = auto()  # default
    IN_PLACE = auto()  # not yet implemented


class OtaClientServerConfig:
    SERVER_PORT = 50051
    WAITING_SUBECU_ACK_REQ_TIMEOUT = 6
    QUERYING_SUBECU_STATUS_TIMEOUT = 30
    LOOP_QUERYING_SUBECU_STATUS_INTERVAL = 10
    STATUS_UPDATE_INTERVAL = 1

    # proxy server
    OTA_PROXY_LISTEN_ADDRESS = "0.0.0.0"
    OTA_PROXY_LISTEN_PORT = 8082


class _InternalSettings:
    """Common internal settings for otaclient.

    WARNING: typically the common settings SHOULD NOT be changed!
             otherwise the backward compatibility will be impact.
    Change the fields in BaseConfig if you want to tune the otaclient.
    """

    # ------ common paths ------ #
    RUN_DIR = "/run/otaclient"
    OTACLIENT_PID_FILE = "/run/otaclient.pid"
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

    # ------ device configuration files ------ #
    # this files should be placed under /boot/ota folder
    ECU_INFO_FNAME = "ecu_info.yaml"
    PROXY_INFO_FNAME = "proxy_info.yaml"

    # ------ ota-status files ------ #
    # this files should be placed under /boot/ota-status folder
    OTA_STATUS_FNAME = "status"
    OTA_VERSION_FNAME = "version"
    SLOT_IN_USE_FNAME = "slot_in_use"

    # ------ otaclient internal used path ------ #
    # standby/refroot mount points
    MOUNT_POINT = "/mnt/standby"
    # where active(old) image partition will be bind mounted to
    ACTIVE_ROOT_MOUNT_POINT = "/mnt/refroot"
    # tmp store for local copy
    OTA_TMP_STORE = "/.ota-tmp"
    # tmp store for standby slot OTA image meta
    OTA_TMP_META_STORE = "/.ota-meta"
    # compressed OTA image support
    SUPPORTED_COMPRESS_ALG: Tuple[str, ...] = ("zst", "zstd")


class BaseConfig(_InternalSettings):
    """User configurable otaclient settings."""

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
        "[%(asctime)s][%(levelname)s]-%(name)s:%(funcName)s:%(lineno)d,%(message)s"
    )

    # ------ otaclient behavior setting ------ #
    # the following settings can be safely changed according to the
    # actual environment otaclient running at.
    # --- file read/write settings --- #
    CHUNK_SIZE = 1 * 1024 * 1024  # 1MB
    LOCAL_CHUNK_SIZE = 4 * 1024 * 1024  # 4MB

    # --- download settings for single download task --- #
    DOWNLOAD_RETRY = 3
    DOWNLOAD_BACKOFF_MAX = 3  # seconds
    DOWNLOAD_BACKOFF_FACTOR = 0.1  # seconds
    # downloader settings
    MAX_DOWNLOAD_THREAD = 7
    DOWNLOADER_CONNPOOL_SIZE_PER_THREAD = 20

    # --- download settings for the whole download tasks group --- #
    # if retry keeps failing without any success in
    # DOWNLOAD_GROUP_NO_SUCCESS_RETRY_TIMEOUT time, failed the whole
    # download task group and raise NETWORK OTA error.
    MAX_CONCURRENT_DOWNLOAD_TASKS = 128
    DOWNLOAD_GROUP_INACTIVE_TIMEOUT = 5 * 60  # seconds
    DOWNLOAD_GROUP_BACKOFF_MAX = 12  # seconds
    DOWNLOAD_GROUP_BACKOFF_FACTOR = 1  # seconds

    # --- stats collector setting --- #
    STATS_COLLECT_INTERVAL = 1  # second

    # --- create standby setting --- #
    # now only REBUILD mode is available
    STANDBY_CREATION_MODE = CreateStandbyMechanism.REBUILD
    MAX_CONCURRENT_PROCESS_FILE_TASKS = 256
    CREATE_STANDBY_RETRY_MAX = 3
    CREATE_STANDBY_BACKOFF_FACTOR = 1
    CREATE_STANDBY_BACKOFF_MAX = 6

    # --- ECU status polling setting --- #
    ECU_STATUS_STORAGE_PROPERTY_UPDATE_INTERVAL = 6  # seconds
    # NOTE: unreachable_timeout should be larger than
    #       downloading_group timeout
    UNREACHABLE_ECU_TIMEOUT = 10 * 60  # seconds
    OTAPROXY_SHUTDOWN_DELAY = 30  # seconds
    ON_RECEIVE_UPDATE_DELAY_ECU_STORAGE_PROPERTIES_UPDATE = 5 * 60  # seconds
    ACTIVE_INTERVAL = 1  # second
    IDLE_INTERVAL = 10  # seconds


# init cfgs
server_cfg = OtaClientServerConfig()
config = BaseConfig()
