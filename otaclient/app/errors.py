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


import traceback
from enum import Enum, unique
from typing import ClassVar

from .proto import wrapper


@unique
class OTAErrorCode(int, Enum):
    E_UNSPECIFIC = 0

    #
    # ------ network related errors ------
    #
    E_NETWORK = 100
    E_OTAMETA_DOWNLOAD_FAILED = 101

    #
    # ------ recoverable errors ------
    #
    E_OTA_ERR_RECOVERABLE = 200
    E_OTA_BUSY = 201
    E_INVALID_STATUS_FOR_OTAROLLBACK = 202

    #
    # ------ unrecoverable errors ------
    #
    E_OTA_ERR_UNRECOVERABLE = 300
    E_BOOTCONTROL_PLATFORM_UNSUPPORTED = 301
    E_BOOTCONTROL_STARTUP_ERR = 302
    E_BOOTCONTROL_PREUPDATE_FAILED = 303
    E_BOOTCONTROL_POSTUPDATE_FAILED = 304
    E_BOOTCONTROL_PREROLLBACK_FAILED = 305
    E_BOOTCONTROL_POSTROLLBACK_FAILED = 306
    E_STANDBY_SLOT_INSUFFICIENT_SPACE = 307
    E_INVALID_OTAUPDATE_REQUEST = 308
    E_METADATAJWT_CERT_VERIFICATION_FAILED = 309
    E_METADATAJWT_INVALID = 310
    E_OTAPROXY_FAILED_TO_START = 311
    E_UPDATEDELTA_GENERATION_FAILED = 312
    E_APPLY_OTAUPDATE_FAILED = 313
    E_OTACLIENT_STARTUP_FAILED = 314

    def to_errcode_str(self) -> str:
        return f"{self.value:0>3}"


class OTAError(Exception):
    """Errors that happen during otaclient code executing.

    This exception class should be the base module level exception for each module.
    It should always be captured by the OTAError at otaclient.py.
    """

    ERROR_PREFIX: ClassVar[str] = "E"

    failure_type: wrapper.FailureType = wrapper.FailureType.RECOVERABLE
    failure_errcode: OTAErrorCode = OTAErrorCode.E_UNSPECIFIC
    failure_description: str = "no description available for this error"

    def __init__(self, *args: object, module: str) -> None:
        self.module = module
        super().__init__(*args)

    @property
    def failure_errcode_str(self) -> str:
        return f"{self.ERROR_PREFIX}{self.failure_errcode.to_errcode_str()}"

    def get_failure_traceback(self, *, splitter="\n") -> str:
        return splitter.join(
            traceback.format_exception(type(self), self, self.__traceback__)
        )

    def get_failure_reason(self) -> str:
        """Return failure_reason str."""
        return f"{self.failure_errcode_str}: {self.failure_description}"

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

    failure_type: wrapper.FailureType = wrapper.FailureType.RECOVERABLE
    failure_errcode: OTAErrorCode = OTAErrorCode.E_NETWORK
    failure_description: str = _NETWORK_ERR_DEFAULT_DESC


class OTAMetaDownloadFailed(NetworkError):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_OTAMETA_DOWNLOAD_FAILED
    failure_description: str = (
        f"failed to download OTA meta due to {_NETWORK_ERR_DEFAULT_DESC}"
    )


#
# ------ recoverable error ------
#

_RECOVERABLE_DEFAULT_DESC = (
    "recoverable OTA error(unrelated to network) detected, "
    "please retry after reboot device or restart otaclient"
)


class OTAErrorRecoverable(OTAError):
    failure_type: wrapper.FailureType = wrapper.FailureType.RECOVERABLE
    failure_errcode: OTAErrorCode = OTAErrorCode.E_OTA_ERR_RECOVERABLE
    failure_description: str = _RECOVERABLE_DEFAULT_DESC


class OTABusy(OTAErrorRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_OTA_BUSY
    failure_description: str = "on-going OTA operation(update or rollback) detected, this request has been ignored"


class InvalidStatusForOTARollback(OTAErrorRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_INVALID_STATUS_FOR_OTAROLLBACK
    failure_description: str = "previous OTA is not succeeded, reject OTA rollback"


#
# ------ recoverable error ------
#

_UNRECOVERABLE_DEFAULT_DESC = (
    "unrecoverable OTA error, please contact technical support"
)


class OTAErrorUnRecoverable(OTAError):
    failure_type: wrapper.FailureType = wrapper.FailureType.RECOVERABLE
    failure_errcode: OTAErrorCode = OTAErrorCode.E_OTA_ERR_UNRECOVERABLE
    failure_description: str = _UNRECOVERABLE_DEFAULT_DESC


class BootControlPlatformUnsupported(OTAErrorUnRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_PLATFORM_UNSUPPORTED
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: bootloader for this ECU is not supported"
    )


class BootControlStartupFailed(OTAErrorUnRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_STARTUP_ERR
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: boot controller startup failed"
    )


class BootControlPreUpdateFailed(OTAErrorUnRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_PREUPDATE_FAILED
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: boot_control pre_update process failed"
    )


class BootControlPostUpdateFailed(OTAErrorUnRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_POSTUPDATE_FAILED
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: boot_control post_update process failed"
    )


class BootControlPreRollbackFailed(OTAErrorUnRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_PREROLLBACK_FAILED
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: boot_control pre_rollback process failed"
    )


class BootControlPostRollbackFailed(OTAErrorUnRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_POSTROLLBACK_FAILED
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: boot_control post_rollback process failed"
    )


class StandbySlotInsufficientSpace(OTAErrorUnRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_STANDBY_SLOT_INSUFFICIENT_SPACE
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: insufficient space at standby slot"
    )


class InvalidUpdateRequest(OTAErrorUnRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_INVALID_OTAUPDATE_REQUEST
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: incoming OTA update request is invalid"
    )


class MetadataJWTInvalid(OTAErrorUnRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_METADATAJWT_INVALID
    failure_description: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: verfication for metadata.jwt is OK but metadata.jwt's content is invalid"


class MetadataJWTVerficationFailed(OTAErrorUnRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_METADATAJWT_CERT_VERIFICATION_FAILED
    failure_description: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: certificate verification failed for OTA metadata.jwt"


class OTAProxyFailedToStart(OTAErrorUnRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_OTAPROXY_FAILED_TO_START
    failure_description: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: otaproxy is required for multiple ECU update but otaproxy failed to start"


class UpdateDeltaGenerationFailed(OTAErrorUnRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_UPDATEDELTA_GENERATION_FAILED
    failure_description: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: failed to calculate and/or prepare update delta"


class ApplyOTAUpdateFailed(OTAErrorUnRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_APPLY_OTAUPDATE_FAILED
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: failed to apply OTA update to standby slot"
    )


class OTAClientStartupFailed(OTAErrorUnRecoverable):
    failure_errcode: OTAErrorCode = OTAErrorCode.E_OTACLIENT_STARTUP_FAILED
    failure_description: str = (
        f"{_UNRECOVERABLE_DEFAULT_DESC}: failed to start otaclient instance"
    )
