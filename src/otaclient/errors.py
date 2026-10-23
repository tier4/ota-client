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
"""OTA error code definition"""

from __future__ import annotations

import contextlib
from enum import Enum, unique
from typing import ClassVar, Generator

from otaclient._types import FailureType
from otaclient._utils import get_traceback
from otaclient_common._typing import StrEnum

MAX_FAILURE_DETAIL_LEN = 256
"""Max length of the detail message attached to the failure_reason.

NOTE: the whole otaclient status(including failure_reason and failure_traceback)
      is pickled into a fixed-size shared memory, keep the detail message short.
"""


class FailureCategory(StrEnum):
    """Who is responsible for resolving the failure, implied by the error code range.

    USER(4xx): caused by the OTA request or the OTA image(invalid request/cookies/image,
        image not matching the ECU, request at wrong timing, etc.).
        The requester side can resolve it by fixing the request or the OTA image.
    SYSTEM(5xx): caused by the ECU, otaclient itself or the environment(network,
        boot control, storage, etc.). Requires investigation on the ECU side.
    """

    USER = "USER"
    SYSTEM = "SYSTEM"


@unique
class OTAErrorCode(int, Enum):
    """OTA error codes, follow the HTTP status code convention.

    4xx: USER side failure, 5xx: SYSTEM side failure.
    The well-known HTTP status code is used when the meaning matches,
        for example, 401(unauthorized), 404(not found), 409(conflict),
        507(insufficient storage).

    NOTE: the error code is exposed via status API, and is used by the upper layer.
          Once released, an error code number MUST NOT be re-assigned to another
          meaning. When an error code is deprecated/moved, mark it as RESERVED_<num>.
    NOTE: codes below 400(1xx/2xx/3xx) are the legacy error codes used by
          previous otaclient versions, they MUST NOT be used anymore.
    NOTE: failure_type(recoverable or not) is NOT implied by the code range,
          it is reported separately via the status API.
    """

    E_UNSPECIFIC = 0

    #
    # ------ 4xx: USER side errors ------
    #
    E_INVALID_OTAUPDATE_REQUEST = 400  # bad request
    E_UPDATE_REQUEST_COOKIE_INVALID = 401  # unauthorized
    E_OTA_IMAGE_NOT_FOUND = 404  # not found
    E_OTA_BUSY = 409  # conflict
    E_INVALID_STATUS_FOR_OTAROLLBACK = 412  # precondition failed
    E_OTA_IMAGE_INVALID = 422  # unprocessable content
    # OTA image verification and compatibility
    E_METADATAJWT_CERT_VERIFICATION_FAILED = 460
    E_METADATAJWT_INVALID = 461
    E_BOOTCONTROL_BSP_VERSION_COMPATIBILITY_FAILED = 462
    # client update
    E_CLIENT_UPDATE_SAME_VERSIONS = 470
    # aborted by the requester(client closed request)
    E_OTA_ABORTED = 499

    #
    # ------ 5xx: SYSTEM side errors ------
    #
    E_OTA_ERR_UNRECOVERABLE = 500  # internal error
    E_BOOTCONTROL_PLATFORM_UNSUPPORTED = 501  # not implemented
    E_NETWORK = 502  # bad gateway
    E_OTA_ERR_RECOVERABLE = 503  # service unavailable
    E_DOWNLOAD_STALLED = 504  # gateway timeout
    E_STANDBY_SLOT_INSUFFICIENT_SPACE = 507  # insufficient storage
    # network and downloading
    E_OTAMETA_DOWNLOAD_FAILED = 520
    E_OTA_RESOURCE_DOWNLOAD_FAILED = 521
    E_OTACLIENT_PACKAGE_DOWNLOAD_FAILED = 522
    E_UPPER_OTAPROXY_UNREACHABLE = 523
    # otaclient runtime
    E_OTACLIENT_STARTUP_FAILED = 530
    E_OTAPROXY_FAILED_TO_START = 531
    E_CA_CERT_NOT_INSTALLED = 532
    E_CLIENT_UPDATE_FAILED = 533
    # OTA update flow(otaclient side)
    E_PREUPDATE_FAILED = 540
    E_UPDATEDELTA_GENERATION_FAILED = 541
    E_APPLY_OTAUPDATE_FAILED = 542
    E_POSTUPDATE_FAILED = 543
    # boot control
    E_BOOTCONTROL_STARTUP_ERR = 550
    E_BOOTCONTROL_PREUPDATE_FAILED = 551
    E_BOOTCONTROL_STANDBY_SLOT_PREPARE_FAILED = 552
    E_BOOTCONTROL_SLOT_MOUNT_FAILED = 553
    E_BOOTCONTROL_POSTUPDATE_FAILED = 554
    E_BOOTCONTROL_BOOT_CONFIG_UPDATE_FAILED = 555
    E_BOOTCONTROL_FIRMWARE_UPDATE_FAILED = 556
    E_BOOTCONTROL_SWITCH_BOOT_FAILED = 557
    E_BOOTCONTROL_PREROLLBACK_FAILED = 558
    E_BOOTCONTROL_POSTROLLBACK_FAILED = 559

    @property
    def category(self) -> FailureCategory:
        if 400 <= self.value < 500:
            return FailureCategory.USER
        return FailureCategory.SYSTEM

    def to_errcode_str(self) -> str:
        return f"{self.value:0>3}"


class OTAError(Exception):
    """Errors that happen during otaclient code executing.

    This exception class should be the base module level exception for each module.
    It should always be captured by the OTAError at otaclient.py.
    """

    ERROR_PREFIX: ClassVar[str] = "E"

    failure_type: FailureType = FailureType.RECOVERABLE
    failure_errcode: OTAErrorCode = OTAErrorCode.E_UNSPECIFIC
    failure_description: str = "no description available for this error"

    def __init__(self, *args: object, module: str) -> None:
        self.module = module
        super().__init__(*args)

    @property
    def failure_category(self) -> FailureCategory:
        return self.failure_errcode.category

    @property
    def failure_errcode_str(self) -> str:
        return f"{self.ERROR_PREFIX}{self.failure_errcode.to_errcode_str()}"

    get_failure_traceback = get_traceback

    @property
    def failure_detail(self) -> str:
        """The detail message passed in when raising this error, truncated to
        at most <MAX_FAILURE_DETAIL_LEN> chars."""
        if not self.args:
            return ""
        _detail = str(self.args[0]) if len(self.args) == 1 else str(self.args)
        if len(_detail) > MAX_FAILURE_DETAIL_LEN:
            _detail = f"{_detail[: MAX_FAILURE_DETAIL_LEN - 3]}..."
        return _detail

    def get_failure_reason(self) -> str:
        """Return failure_reason str.

        Format: `E<errcode>: <failure_description>[; detail: <failure_detail>]`
        """
        _reason = f"{self.failure_errcode_str}: {self.failure_description}"
        if _detail := self.failure_detail:
            return f"{_reason}; detail: {_detail}"
        return _reason

    def get_error_report(self, title: str = "") -> str:
        """The detailed failure report for debug use."""
        return (
            f"\n{title}\n"
            f"@module: {self.module}"
            "\n------ failure_reason ------\n"
            f"{self.get_failure_reason()}"
            "\n------ end of failure_reason ------\n"
            "\n------ exception informaton ------\n"
            f"{self!r}"
            "\n------ end of exception informaton ------\n"
            "\n------ exception traceback ------\n"
            f"{self.get_failure_traceback()}"
            "\n------ end of exception traceback ------\n"
        )


#
# ------ Network related error ------
#

_NETWORK_ERR_DEFAULT_DESC = "network unstable, please check the network connection"


class NetworkError(OTAError):
    """Generic network error"""

    failure_type = FailureType.RECOVERABLE
    failure_errcode = OTAErrorCode.E_NETWORK
    failure_description = _NETWORK_ERR_DEFAULT_DESC


class OTAMetaDownloadFailed(NetworkError):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_OTAMETA_DOWNLOAD_FAILED
    failure_description: str = (
        f"failed to download OTA meta due to {_NETWORK_ERR_DEFAULT_DESC}"
    )


class OTAClientPackageDownloadFailed(NetworkError):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_OTACLIENT_PACKAGE_DOWNLOAD_FAILED
    failure_description: str = (
        f"failed to download OTAClient package due to {_NETWORK_ERR_DEFAULT_DESC}"
    )


class OTAResourceDownloadFailed(NetworkError):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_OTA_RESOURCE_DOWNLOAD_FAILED
    failure_description: str = (
        f"failed to download OTA image resources due to {_NETWORK_ERR_DEFAULT_DESC}"
    )


class DownloadStalled(NetworkError):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_DOWNLOAD_STALLED
    failure_description: str = (
        "downloading made no progress for too long, "
        "please check the network connection to the OTA image server or upper otaproxy"
    )


class UpperOTAProxyUnreachable(NetworkError):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_UPPER_OTAPROXY_UNREACHABLE
    failure_description: str = (
        "upper otaproxy is not reachable, "
        "please check the main ECU's otaproxy and the in-vehicle network"
    )


#
# ------ recoverable error ------
#

_RECOVERABLE_DEFAULT_DESC = (
    "recoverable OTA error(unrelated to network) detected, "
    "please retry after reboot device or restart otaclient"
)


class OTAErrorRecoverable(OTAError):
    failure_type = FailureType.RECOVERABLE
    failure_errcode = OTAErrorCode.E_OTA_ERR_RECOVERABLE
    failure_description = _RECOVERABLE_DEFAULT_DESC


class OTABusy(OTAErrorRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_OTA_BUSY
    failure_description: str = "on-going OTA operation(update or rollback) detected, this request has been ignored"


class InvalidStatusForOTARollback(OTAErrorRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_INVALID_STATUS_FOR_OTAROLLBACK
    failure_description: str = "previous OTA is not succeeded, reject OTA rollback"


class OTAImageInvalid(OTAErrorRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_OTA_IMAGE_INVALID
    failure_description: str = "failed to complete OTA as OTA image is broken"


class OTAImageNotFound(OTAImageInvalid):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_OTA_IMAGE_NOT_FOUND
    failure_description: str = (
        "failed to complete OTA as OTA image(or some of its files) is not found(HTTP 404), "
        "please check the OTA image URL"
    )


class UpdateRequestCookieInvalid(OTAErrorRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_UPDATE_REQUEST_COOKIE_INVALID
    failure_description: str = "failed to complete OTA as cookie is invalid"


class ClientUpdateSameVersions(OTAErrorRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_CLIENT_UPDATE_SAME_VERSIONS
    failure_description: str = "client package version is the same, skip client update"


class ClientUpdateFailed(OTAErrorRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_CLIENT_UPDATE_FAILED
    failure_description: str = (
        "failed to update client package, please check the log for more details"
    )


class OTAAbortSignal(OTAErrorRecoverable):
    """Control-flow signal for abort stack unwinding.

    Raised by AbortHandler zone transition methods (enter_critical_zone,
    exit_critical_zone, enter_final_phase) when abort is in progress.
    Caught by execute() to avoid calling on_operation_failure().
    """

    failure_errcode: OTAErrorCode = OTAErrorCode.E_OTA_ABORTED
    failure_description: str = "OTA abort signal, cleanup pending"


class BootControlBSPVersionCompatibilityFailed(OTAErrorRecoverable):
    failure_errcode: OTAErrorCode = (
        OTAErrorCode.E_BOOTCONTROL_BSP_VERSION_COMPATIBILITY_FAILED
    )
    failure_description: str = "boot_control BSP version compatibility check failed"


#
# ------ unrecoverable error ------
#

_UNRECOVERABLE_DEFAULT_DESC = (
    "unrecoverable OTA error, please contact technical support"
)


class OTAErrorUnrecoverable(OTAError):
    failure_type = FailureType.UNRECOVERABLE
    failure_errcode = OTAErrorCode.E_OTA_ERR_UNRECOVERABLE
    failure_description = _UNRECOVERABLE_DEFAULT_DESC


class BootControlPlatformUnsupported(OTAErrorUnrecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_PLATFORM_UNSUPPORTED
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: bootloader for this ECU is not supported"
    )


class BootControlStartupFailed(OTAErrorUnrecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_STARTUP_ERR
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: boot controller startup failed"
    )


class BootControlPreUpdateFailed(OTAErrorUnrecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_PREUPDATE_FAILED
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: boot_control pre_update process failed"
    )


class BootControlPostUpdateFailed(OTAErrorUnrecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_POSTUPDATE_FAILED
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: boot_control post_update process failed"
    )


class BootControlPreRollbackFailed(OTAErrorUnrecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_PREROLLBACK_FAILED
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: boot_control pre_rollback process failed"
    )


class BootControlPostRollbackFailed(OTAErrorUnrecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_POSTROLLBACK_FAILED
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: boot_control post_rollback process failed"
    )


class StandbySlotInsufficientSpace(OTAErrorUnrecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_STANDBY_SLOT_INSUFFICIENT_SPACE
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: insufficient space at standby slot"
    )


class InvalidUpdateRequest(OTAErrorUnrecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_INVALID_OTAUPDATE_REQUEST
    failure_description: str = (
        "incoming OTA update request is invalid, please check the request"
    )


class MetadataJWTInvalid(OTAErrorUnrecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_METADATAJWT_INVALID
    failure_description: str = "verfication for metadata.jwt is OK but metadata.jwt's content is invalid, please check the OTA image"


class MetadataJWTVerficationFailed(OTAErrorUnrecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_METADATAJWT_CERT_VERIFICATION_FAILED
    failure_description: str = "certificate verification failed for OTA metadata.jwt, please check the OTA image is signed for this ECU"


class CACertNotInstalled(MetadataJWTVerficationFailed):
    """No CA cert is installed on this ECU, OTA image cannot be verified."""

    failure_errcode: OTAErrorCode = OTAErrorCode.E_CA_CERT_NOT_INSTALLED
    failure_description: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: no CA cert is installed on this ECU, cannot verify OTA image"


class OTAProxyFailedToStart(OTAErrorUnrecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_OTAPROXY_FAILED_TO_START
    failure_description: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: otaproxy is required for multiple ECU update but otaproxy failed to start"


class UpdateDeltaGenerationFailed(OTAErrorUnrecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_UPDATEDELTA_GENERATION_FAILED
    failure_description: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: failed to calculate and/or prepare update delta"


class ApplyOTAUpdateFailed(OTAErrorUnrecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_APPLY_OTAUPDATE_FAILED
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: failed to apply OTA update to standby slot"
    )


class OTAClientStartupFailed(OTAErrorUnrecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_OTACLIENT_STARTUP_FAILED
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: failed to start otaclient instance"
    )


class PreUpdateFailed(OTAErrorUnrecoverable):
    """otaclient side pre-update failure, boot control failures are not included."""

    failure_errcode: OTAErrorCode = OTAErrorCode.E_PREUPDATE_FAILED
    failure_description: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: failed to prepare standby slot for applying update"


class PostUpdateFailed(OTAErrorUnrecoverable):
    """otaclient side post-update failure, boot control failures are not included."""

    failure_errcode: OTAErrorCode = OTAErrorCode.E_POSTUPDATE_FAILED
    failure_description: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: failed to finish post-update process(persist files, OTA image metadata) on standby slot"


#
# ------ sub-categories of boot control pre_update/post_update failure ------
#
# NOTE: these errors are the sub-classes of BootControlPreUpdateFailed and
#       BootControlPostUpdateFailed, so the existing exception handlings still apply.


class BootControlStandbySlotPrepareFailed(BootControlPreUpdateFailed):
    failure_errcode: OTAErrorCode = (
        OTAErrorCode.E_BOOTCONTROL_STANDBY_SLOT_PREPARE_FAILED
    )
    failure_description: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: boot_control failed to prepare(format) standby slot device"


class BootControlSlotMountFailed(BootControlPreUpdateFailed):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_SLOT_MOUNT_FAILED
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: boot_control failed to mount slots"
    )


class BootControlBootConfigUpdateFailed(BootControlPostUpdateFailed):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_BOOT_CONFIG_UPDATE_FAILED
    failure_description: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: boot_control failed to update boot configurations(fstab, bootloader config, kernel files) for standby slot"


class BootControlFirmwareUpdateFailed(BootControlPostUpdateFailed):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_FIRMWARE_UPDATE_FAILED
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: boot_control failed to update firmware"
    )


class BootControlSwitchBootFailed(BootControlPostUpdateFailed):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_SWITCH_BOOT_FAILED
    failure_description: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: boot_control failed to switch boot to standby slot"


#
# ------ helpers ------
#


@contextlib.contextmanager
def raise_as_ota_error(
    error_cls: type[OTAError], err_msg: str, *, module: str
) -> Generator[None]:
    """Convert any non-OTAError exception raised within this context into <error_cls>.

    OTAError(including OTAAbortSignal) raised within this context is re-raised as is,
        so the more specific error code from the inner code path will be kept.
    """
    try:
        yield
    except OTAError:
        raise
    except Exception as e:
        raise error_cls(f"{err_msg}: {e!r}", module=module) from e
