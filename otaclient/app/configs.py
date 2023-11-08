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
import logging
import os.path
from enum import Enum
from os.path import isabs, isdir
from pathlib import Path
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    IPvAnyAddress,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import ClassVar, Dict, Tuple
from typing_extensions import Annotated

from otaclient import __file__ as _otaclient__init__
from otaclient._utils import cached_computed_field, validator_wrapper
from otaclient._utils.path import replace_root
from otaclient._utils.logging import check_loglevel

OTACLIENT_PACKAGE_ROOT = Path(_otaclient__init__).parent

# NOTE: VERSION file is installed under otaclient package root
EXTRA_VERSION_FILE = str(OTACLIENT_PACKAGE_ROOT / "version.txt")


class CreateStandbyMechanism(str, Enum):
    LEGACY = "legacy"  # deprecated and removed
    REBUILD = "rebuild"  # current default
    IN_PLACE = "in_place"  # not yet implemented


class _FixedInternalConfigs(BaseModel):
    """Fixed internal configs."""

    RUN_DPATH: str = "/run/otaclient"
    OTACLIENT_PID_FPATH: str = "/run/otaclient.pid"
    SUPPORTED_COMPRESS_ALG: ClassVar[Tuple[str, ...]] = ("zst", "zstd")

    EXTERNAL_CACHE_DEV_FSLABEL: ClassVar[str] = "ota_cache_src"


class _DynamicRootedPathsConfig(BaseModel):
    """Dynamic generated internal paths config.

    Paths configured in this class are dynamically adjusted with specified
    ACTIVE_ROOTFS.
    """

    #
    # --- active_rootfs & container mode ---
    #
    DEFAULT_ACTIVE_ROOTFS: ClassVar[str] = "/"
    ACTIVE_ROOTFS: Annotated[
        str,
        AfterValidator(validator_wrapper(isabs)),
        AfterValidator(validator_wrapper(isdir)),
    ] = DEFAULT_ACTIVE_ROOTFS

    @cached_computed_field
    def IS_CONTAINER(self) -> bool:
        """Whether otaclient is running as container.

        If active rootfs is specified and not /, otaclient will
        activate the container running mode.
        """
        return self.ACTIVE_ROOTFS != self.DEFAULT_ACTIVE_ROOTFS

    #
    # --- mount point placement ---
    #
    DEFAULT_OTACLIENT_MOUNT_SPACE: ClassVar[str] = "/mnt/otaclient"

    @cached_computed_field
    def OTACLIENT_MOUNT_SPACE_DPATH(self) -> str:
        return replace_root(
            self.DEFAULT_OTACLIENT_MOUNT_SPACE,
            self.DEFAULT_ACTIVE_ROOTFS,
            self.ACTIVE_ROOTFS,
        )

    @cached_computed_field
    def STANDBY_SLOT_MP(self) -> str:
        return os.path.join(self.OTACLIENT_MOUNT_SPACE_DPATH, "standby_slot")

    @cached_computed_field
    def ACTIVE_SLOT_MP(self) -> str:
        return os.path.join(self.OTACLIENT_MOUNT_SPACE_DPATH, "active_slot")

    #
    # --- /boot related ---
    #
    @cached_computed_field
    def BOOT_DPATH(self) -> str:
        return os.path.join(self.ACTIVE_ROOTFS, "boot")

    # /boot/ota and its files

    @cached_computed_field
    def BOOT_OTA_DPATH(self) -> str:
        return os.path.join(self.BOOT_DPATH, "ota")

    @cached_computed_field
    def ECU_INFO_FPATH(self) -> str:
        return os.path.join(self.BOOT_OTA_DPATH, "ecu_info.yaml")

    @cached_computed_field
    def PROXY_INFO_FPATH(self) -> str:
        return os.path.join(self.BOOT_OTA_DPATH, "proxy_info.yaml")

    # /boot/ota-status and its files

    OTA_STATUS_FNAME: ClassVar[str] = "status"
    OTA_VERSION_FNAME: ClassVar[str] = "version"
    SLOT_IN_USE_FNAME: ClassVar[str] = "slot_in_use"

    # some files under /etc

    @cached_computed_field
    def ETC_DPATH(self) -> str:
        return os.path.join(self.ACTIVE_ROOTFS, "etc")

    @cached_computed_field
    def PASSWD_FPATH(self) -> str:
        return os.path.join(self.ETC_DPATH, "passwd")

    @cached_computed_field
    def GROUP_FPATH(self) -> str:
        return os.path.join(self.ETC_DPATH, "group")

    @cached_computed_field
    def FSTAB_FPATH(self) -> str:
        return os.path.join(self.ETC_DPATH, "fstab")

    #
    # ------ /opt/ota paths ------
    #
    DEFAULT_OTA_CERTS_DPATHS: ClassVar[str] = "/opt/ota/client/certs"
    DEFAULT_OTA_INSTALLATION_PATH: ClassVar[str] = "/opt/ota"

    @cached_computed_field
    def OTA_INSTALLATION_PATH(self) -> str:
        return replace_root(
            self.DEFAULT_OTA_INSTALLATION_PATH,
            self.DEFAULT_ACTIVE_ROOTFS,
            self.ACTIVE_ROOTFS,
        )

    @cached_computed_field
    def OTA_CERTS_DPATH(self) -> str:
        return replace_root(
            self.DEFAULT_OTA_CERTS_DPATHS,
            self.DEFAULT_ACTIVE_ROOTFS,
            self.ACTIVE_ROOTFS,
        )

    @cached_computed_field
    def OTACLIENT_INSTALLATION_PATH(self) -> str:
        return os.path.join(self.OTA_INSTALLATION_PATH, "client")

    @cached_computed_field
    def ACTIVE_IMAGE_META_DPATH(self) -> str:
        return os.path.join(self.OTA_INSTALLATION_PATH, "image-meta")

    @cached_computed_field
    def STANDBY_IMAGE_META_DPATH(self) -> str:
        return replace_root(
            self.ACTIVE_IMAGE_META_DPATH, self.ACTIVE_ROOTFS, self.STANDBY_SLOT_MP
        )

    #
    # ------ external OTA cache source support ------
    #
    @cached_computed_field
    def EXTERNAL_CACHE_DEV_MOUNTPOINT(self) -> str:
        return os.path.join(self.OTACLIENT_MOUNT_SPACE_DPATH, "external_cache_src")

    @cached_computed_field
    def EXTERNAL_CACHE_SRC_PATH(self) -> str:
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

    # enable internal debug feature
    # currently available feature:
    #   1. enable failure_traceback in status API response
    DEBUG_MODE: bool = False

    # OTA used tmp folder on standby slot
    OTA_TMP_DPATH: str = "/ota-tmp"

    #
    # ------ otaclient grpc server config ------
    #
    SERVER_ADDRESS: IPvAnyAddress = IPvAnyAddress("0.0.0.0")
    SERVER_PORT: int = Field(default=50051, ge=0, le=65535)

    #
    # ------ otaproxy server config ------
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

    #
    # --- request dispatch settings ---
    #
    WAITING_SUBECU_ACK_REQ_TIMEOUT: int = 6
    QUERYING_SUBECU_STATUS_TIMEOUT: int = 30
    LOOP_QUERYING_SUBECU_STATUS_INTERVAL: int = 10

    #
    # --- file I/O settings --- #
    #
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

    # --- default version str ---
    DEFAULT_VERSION_STR: str = ""


class Config(_InternalConfigs, _NormalConfigs):
    model_config = ConfigDict(frozen=True, validate_default=True)

    @cached_computed_field
    def STANDBY_OTA_TMP_DPATH(self) -> str:
        return replace_root(
            self.OTA_TMP_DPATH, self.DEFAULT_ACTIVE_ROOTFS, self.STANDBY_SLOT_MP
        )


# init config

ENV_PREFIX = "OTA_"
HOST_ROOTFS_ENV = f"{ENV_PREFIX}HOST_ROOTFS"


def _init_config() -> Config:
    class _ConfigurableNormalConfigs(BaseSettings, _NormalConfigs):
        """one-time class that parse configs from environment vars."""

        model_config = SettingsConfigDict(env_prefix=ENV_PREFIX)

    return Config(
        ACTIVE_ROOTFS=os.getenv(
            HOST_ROOTFS_ENV, _DynamicRootedPathsConfig.DEFAULT_ACTIVE_ROOTFS
        ),
        **_ConfigurableNormalConfigs().model_dump(),
    )


config = _init_config()
del _init_config
