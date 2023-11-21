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
"""Runtime configs and consts for otaclient."""


from __future__ import annotations
import logging
import os.path
from enum import Enum
from pathlib import Path
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    IPvAnyAddress,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import TYPE_CHECKING, Any, ClassVar as _std_ClassVar, Dict
from typing_extensions import Annotated

from otaclient import __file__ as _otaclient__init__
from otaclient._utils import cached_computed_field
from otaclient._utils.path import replace_root
from otaclient._utils.logging import check_loglevel

# A simple trick to make plain ClassVar work when
# __future__.annotations is activated.
if not TYPE_CHECKING:
    _std_ClassVar = _std_ClassVar[Any]


OTACLIENT_PACKAGE_ROOT = Path(_otaclient__init__).parent

# NOTE: VERSION file is installed under otaclient package root
EXTRA_VERSION_FILE = str(OTACLIENT_PACKAGE_ROOT / "version.txt")


class CreateStandbyMechanism(str, Enum):
    LEGACY = "legacy"  # deprecated and removed
    REBUILD = "rebuild"  # current default
    IN_PLACE = "in_place"  # not yet implemented


class _FixedInternalConfigs(BaseModel):
    """Fixed internal configs."""

    RUN_DPATH: _std_ClassVar = "/run/otaclient"
    OTACLIENT_PID_FPATH: _std_ClassVar = "/run/otaclient.pid"
    SUPPORTED_COMPRESS_ALG: _std_ClassVar = ("zst", "zstd")

    # filesystem label of external cache source
    EXTERNAL_CACHE_DEV_FSLABEL: _std_ClassVar = "ota_cache_src"


class _DynamicRootedPathsConfig(BaseModel):
    """Dynamic generated internal paths config.

    Paths configured in this class are dynamically adjusted and rooted with
    specified ACTIVE_ROOTFS, by default ACTIVE_ROOTFS is "/".
    """

    #
    # ------ active_rootfs & container mode ------ #
    #
    DEFAULT_ACTIVE_ROOTFS: _std_ClassVar = "/"

    ACTIVE_ROOTFS: str = DEFAULT_ACTIVE_ROOTFS

    @cached_computed_field
    def IS_CONTAINER(self) -> bool:
        """Whether otaclient is running as container.

        If active rootfs is specified and not "/", otaclient will
        activate the container running mode.
        """
        return self.ACTIVE_ROOTFS != self.DEFAULT_ACTIVE_ROOTFS

    #
    # ------ mount point placement ------ #
    #
    DEFAULT_OTACLIENT_MOUNT_SPACE: _std_ClassVar = "/mnt/otaclient"

    @cached_computed_field
    def OTACLIENT_MOUNT_SPACE_DPATH(self) -> str:
        """The dynamically rooted location to hold mount points created by otaclient.

        Default: /mnt/otaclient
        """
        return replace_root(
            self.DEFAULT_OTACLIENT_MOUNT_SPACE,
            self.DEFAULT_ACTIVE_ROOTFS,
            self.ACTIVE_ROOTFS,
        )

    @cached_computed_field
    def STANDBY_SLOT_MP(self) -> str:
        """The dynamically rooted location to mount standby slot partition.

        Default: /mnt/otaclient/standby_slot
        """
        return os.path.join(self.OTACLIENT_MOUNT_SPACE_DPATH, "standby_slot")

    @cached_computed_field
    def ACTIVE_SLOT_MP(self) -> str:
        """The dynamically rooted location to mount active slot partition.

        Default: /mnt/otaclient/active_slot
        """
        return os.path.join(self.OTACLIENT_MOUNT_SPACE_DPATH, "active_slot")

    #
    # ------ /boot related ------ #
    #
    @cached_computed_field
    def BOOT_DPATH(self) -> str:
        """The dynamically rooted location of /boot dir.

        Default: /boot
        """
        return os.path.join(self.ACTIVE_ROOTFS, "boot")

    # /boot/ota and its files

    @cached_computed_field
    def BOOT_OTA_DPATH(self) -> str:
        """The dynamically rooted location holding proxy_info.yaml and ecu_info.yaml.

        Default: /boot/ota
        """
        return os.path.join(self.BOOT_DPATH, "ota")

    @cached_computed_field
    def ECU_INFO_FPATH(self) -> str:
        """The dynamically rooted location of ecu_info.yaml.

        Default: /boot/ota/ecu_info.yaml
        """
        return os.path.join(self.BOOT_OTA_DPATH, "ecu_info.yaml")

    @cached_computed_field
    def PROXY_INFO_FPATH(self) -> str:
        """The dynamically rooted location of proxy_info.yaml.

        Default: /boot/ota/proxy_info.yaml
        """
        return os.path.join(self.BOOT_OTA_DPATH, "proxy_info.yaml")

    # --- /boot/ota-status and its files --- #
    # NOTE: the actual location of these files depends on each boot controller
    #       implementation, please refer to boot_control.configs.

    OTA_STATUS_FNAME: _std_ClassVar = "status"
    OTA_VERSION_FNAME: _std_ClassVar = "version"
    SLOT_IN_USE_FNAME: _std_ClassVar = "slot_in_use"

    # --- some files under /etc --- #

    @cached_computed_field
    def ETC_DPATH(self) -> str:
        """The dynamically rooted location of /etc folder.

        Default: /etc
        """
        return os.path.join(self.ACTIVE_ROOTFS, "etc")

    @cached_computed_field
    def PASSWD_FPATH(self) -> str:
        """The dynamically rooted location of /etc/passwd file.

        Default: /etc/passwd
        """
        return os.path.join(self.ETC_DPATH, "passwd")

    @cached_computed_field
    def GROUP_FPATH(self) -> str:
        """The dynamically rooted location of /etc/group file.

        Default: /etc/group
        """
        return os.path.join(self.ETC_DPATH, "group")

    @cached_computed_field
    def FSTAB_FPATH(self) -> str:
        """The dynamically rooted location of /etc/fstab file.

        Default: /etc/fstab
        """
        return os.path.join(self.ETC_DPATH, "fstab")

    #
    # ------ /opt/ota paths ------ #
    #
    # This folder holds files and packages related to OTA functionality.

    DEFAULT_OTA_CERTS_DPATHS: _std_ClassVar = "/opt/ota/client/certs"
    DEFAULT_OTA_INSTALLATION_PATH: _std_ClassVar = "/opt/ota"

    @cached_computed_field
    def OTA_INSTALLATION_PATH(self) -> str:
        """The dynamically rooted OTA installation path.

        Default: /opt/ota
        """
        return replace_root(
            self.DEFAULT_OTA_INSTALLATION_PATH,
            self.DEFAULT_ACTIVE_ROOTFS,
            self.ACTIVE_ROOTFS,
        )

    @cached_computed_field
    def OTA_CERTS_DPATH(self) -> str:
        """The dynamically rooted location of certs for OTA metadata validation.

        Default: /opt/ota/client/certs
        """
        return replace_root(
            self.DEFAULT_OTA_CERTS_DPATHS,
            self.DEFAULT_ACTIVE_ROOTFS,
            self.ACTIVE_ROOTFS,
        )

    @cached_computed_field
    def OTACLIENT_INSTALLATION_PATH(self) -> str:
        """The dynamically rooted location of otaclient installation path.

        Default: /opt/ota/client
        """
        return os.path.join(self.OTA_INSTALLATION_PATH, "client")

    @cached_computed_field
    def ACTIVE_IMAGE_META_DPATH(self) -> str:
        """The dynamically rooted location of the image-meta of active slot.

        Default: /opt/ota/image-meta
        """
        return os.path.join(self.OTA_INSTALLATION_PATH, "image-meta")

    @cached_computed_field
    def STANDBY_IMAGE_META_DPATH(self) -> str:
        """The location of save destination of standby slot.

        NOTE: this location is relatived to the standby slot's mount point.

        Default: /mnt/otaclient/standby_slot/opt/ota/image-meta
        """
        return replace_root(
            self.ACTIVE_IMAGE_META_DPATH, self.ACTIVE_ROOTFS, self.STANDBY_SLOT_MP
        )

    #
    # ------ external OTA cache source support ------
    #
    @cached_computed_field
    def EXTERNAL_CACHE_DEV_MOUNTPOINT(self) -> str:
        """The mount point for external OTA cache source partition.

        Default: /mnt/otaclient/external_cache_src
        """
        return os.path.join(self.OTACLIENT_MOUNT_SPACE_DPATH, "external_cache_src")

    @cached_computed_field
    def EXTERNAL_CACHE_SRC_PATH(self) -> str:
        """The data folder of the external cache source filesystem.

        NOTE: this path is relatived to the external cache source mount point.

        Default: /mnt/otaclient/external_cache_src/data
        """
        return os.path.join(self.EXTERNAL_CACHE_DEV_MOUNTPOINT, "data")


class _InternalConfigs(_FixedInternalConfigs, _DynamicRootedPathsConfig):
    """Internal configs for otaclient.

    User should not change these settings, except ACTIVE_ROOTFS if running as container,
    otherwise otaclient might not work properly or backward-compatibility breaks.
    """


class _NormalConfigs(BaseModel):
    """User configurable otaclient settings.

    These settings can tune the runtime performance and behavior of otaclient,
    configurable via environment variables, with prefix OTA.
    For example, to set SERVER_ADDRESS, set env OTA_SERVER_ADDRESS=10.0.1.1 .
    """

    #
    # ------ enable internal debug feature ------ #
    #

    # main DEBUG_MODE switch, this flag will enable all debug feature.
    DEBUG_MODE: bool = False

    # enable failure_traceback field in status API response.
    DEBUG_ENABLE_TRACEBACK_IN_STATUS_API: bool = False

    # name of OTA used temp folder
    OTA_TMP_DNAME: str = "ota_tmp"

    #
    # ------ otaclient grpc server config ------ #
    #
    SERVER_ADDRESS: IPvAnyAddress = IPvAnyAddress("0.0.0.0")
    SERVER_PORT: int = Field(default=50051, ge=0, le=65535)

    #
    # ------ otaproxy server config ------ #
    #
    OTA_PROXY_LISTEN_ADDRESS: IPvAnyAddress = IPvAnyAddress("0.0.0.0")
    OTA_PROXY_LISTEN_PORT: int = Field(default=8082, ge=0, le=65535)

    #
    # ------ otaclient logging setting ------ #
    #
    LOGGING_LEVEL: Annotated[int, AfterValidator(check_loglevel)] = logging.INFO
    LOG_LEVEL_TABLE: Dict[str, Annotated[int, AfterValidator(check_loglevel)]] = {
        "otaclient.app.boot_control.cboot": LOGGING_LEVEL,
        "otaclient.app.boot_control.grub": LOGGING_LEVEL,
        "otaclient.app.ota_client": LOGGING_LEVEL,
        "otaclient.app.ota_client_service": LOGGING_LEVEL,
        "otaclient.app.ota_client_stub": LOGGING_LEVEL,
        "otaclient.app.ota_metadata": LOGGING_LEVEL,
        "otaclient.app.downloader": LOGGING_LEVEL,
        "otaclient.app.main": LOGGING_LEVEL,
    }
    LOG_FORMAT: str = (
        "[%(asctime)s][%(levelname)s]-%(name)s:%(funcName)s:%(lineno)d,%(message)s"
    )

    #
    # ------ otaclient runtime behavior setting ------ #
    #

    # --- request dispatch settings --- #
    WAITING_SUBECU_ACK_REQ_TIMEOUT: int = 6
    QUERYING_SUBECU_STATUS_TIMEOUT: int = 30
    LOOP_QUERYING_SUBECU_STATUS_INTERVAL: int = 10

    # --- file I/O settings --- #
    CHUNK_SIZE: int = 1 * 1024 * 1024  # 1MB
    LOCAL_CHUNK_SIZE: int = 4 * 1024 * 1024  # 4MB

    #
    # --- download settings for single download task --- #
    #
    DOWNLOAD_RETRY: int = 3
    DOWNLOAD_BACKOFF_MAX: int = 3  # seconds
    DOWNLOAD_BACKOFF_FACTOR: float = 0.1  # seconds

    #
    # --- downloader settings --- #
    #
    MAX_DOWNLOAD_THREAD: int = Field(default=7, le=32)
    DOWNLOADER_CONNPOOL_SIZE_PER_THREAD: int = Field(default=20, le=64)

    #
    # --- download settings for the whole download tasks group --- #
    #
    # if retry keeps failing without any success in
    # DOWNLOAD_GROUP_NO_SUCCESS_RETRY_TIMEOUT time, failed the whole
    # download task group and raise NETWORK OTA error.
    MAX_CONCURRENT_DOWNLOAD_TASKS: int = Field(default=128, le=1024)
    DOWNLOAD_GROUP_INACTIVE_TIMEOUT: int = 5 * 60  # seconds
    DOWNLOAD_GROUP_BACKOFF_MAX: int = 12  # seconds
    DOWNLOAD_GROUP_BACKOFF_FACTOR: int = 1  # seconds

    #
    # --- stats collector setting --- #
    #
    STATS_COLLECT_INTERVAL: int = 1  # second

    #
    # --- create standby setting --- #
    #
    # now only REBUILD mode is available
    STANDBY_CREATION_MODE: CreateStandbyMechanism = CreateStandbyMechanism.REBUILD
    MAX_CONCURRENT_PROCESS_FILE_TASKS: int = Field(default=256, le=2048)
    CREATE_STANDBY_RETRY_MAX: int = 3
    CREATE_STANDBY_BACKOFF_FACTOR: int = 1
    CREATE_STANDBY_BACKOFF_MAX: int = 6

    #
    # --- ECU status polling setting, otaproxy dependency managing --- #
    #
    # The ECU status storage will summarize the stored ECUs' status report
    # and generate overall status report for all ECUs every <INTERVAL> seconds.
    OVERALL_ECUS_STATUS_UPDATE_INTERVAL: int = 6  # seconds

    # If ECU has been disconnected longer than <TIMEOUT> seconds, it will be
    # treated as UNREACHABLE, and will not be counted when generating overall
    # ECUs status report.
    # NOTE: unreachable_timeout should be larger than
    #       downloading_group timeout
    ECU_UNREACHABLE_TIMEOUT: int = 20 * 60  # seconds

    # Otaproxy should not be shutdowned with less than <INTERVAL> seconds
    # after it just starts to prevent repeatedly start/stop cycle.
    OTAPROXY_MINIMUM_SHUTDOWN_INTERVAL: int = 1 * 60  # seconds

    # When any ECU acks update request, this ECU will directly set the overall ECU status
    # to any_in_update=True, any_requires_network=True, all_success=False, to prevent
    # pre-mature overall ECU status changed caused by child ECU delayed ack to update request.
    #
    # This pre-set overall ECU status will be kept for <KEEP_TIME> seconds.
    # This value is expected to be larger than the time cost for subECU acks the OTA request.
    KEEP_OVERALL_ECUS_STATUS_ON_ANY_UPDATE_REQ_ACKED: int = 60  # seconds

    # Active status polling interval, when there is active OTA update in the cluster.
    ACTIVE_INTERVAL: int = 1  # second

    # Idle status polling interval, when ther is no active OTA updaste in the cluster.
    IDLE_INTERVAL: int = 10  # seconds

    #
    # --- default version str --- #
    #
    # The string return in status API firmware_version field if version is unknown.
    DEFAULT_VERSION_STR: str = ""


class Config(_InternalConfigs, _NormalConfigs):
    model_config = ConfigDict(frozen=True, validate_default=True)

    @cached_computed_field
    def STANDBY_OTA_TMP_DPATH(self) -> str:
        """The location for holding OTA runtime files during OTA.

        NOTE: this location is relatived to standby slot's mount point.

        Default: /mnt/otaclient/standby_slot/ota_tmp
        """
        return os.path.join(self.STANDBY_SLOT_MP, self.OTA_TMP_DNAME)


#
# ------ init config ------ #
#

ENV_PREFIX = "OTA_"
# NOTE: ACTIVE_ROOTFS is specially treated and retrieved via HOST_ROOTFS_ENV.
HOST_ROOTFS_ENV = f"{ENV_PREFIX}HOST_ROOTFS"


def _init_config() -> Config:
    class _ConfigurableNormalConfigs(BaseSettings, _NormalConfigs):
        """one-time class that parse configs from environment vars."""

        # retrieve user configurable configs from environmental variables
        model_config = SettingsConfigDict(env_prefix=ENV_PREFIX)

    return Config(
        # especially get ACTIVE_ROOTFS via HOST_ROOTFS_ENV.
        ACTIVE_ROOTFS=os.getenv(
            HOST_ROOTFS_ENV, _DynamicRootedPathsConfig.DEFAULT_ACTIVE_ROOTFS
        ),
        **_ConfigurableNormalConfigs().model_dump(),
    )


config = _init_config()
del _init_config
