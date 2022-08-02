"""OTA error code definition"""
import traceback
from enum import Enum, unique

from app.proto import wrapper


@unique
class OTAModules(Enum):
    General = 0
    BootController = 1
    StandbySlotCreater = 2
    Downloader = 3
    API = 4

    def to_str(self) -> str:
        return f"{self.value:0>2}"


@unique
class OTAAPI(Enum):
    Unspecific = 0
    Update = 1
    Rollback = 2

    def to_str(self) -> str:
        return f"{self.value:0>2}"


class OTAError(Exception):
    """Errors that happen during otaclient code executing.

    This exception class should be the base module level exception for each module.
    It should always be captured by the OTAError at otaclient.py.
    """

    failure_type: wrapper.FailureType
    module: OTAModules
    errcode: "OTAErrorCode"
    desc: str = "no description available for this error"


class OTA_APIError(Exception):
    """Errors that happen during processing API request.

    This exception class should be the top level exception for each API entry.
    This exception must be created by wrapping an OTAClientError.
    """

    api: OTAAPI
    _err_prefix = "E"

    def __init__(self, ota_err: OTAError, *args: object) -> None:
        super().__init__(*args)
        self.otaclient_err = ota_err
        self.errcode = ota_err.errcode
        self.module = ota_err.module
        self.failure_type = ota_err.failure_type
        self.errdesc = ota_err.desc

    def get_errcode(self) -> "OTAErrorCode":
        return self.errcode

    def get_errcode_str(self) -> str:
        return f"{self._err_prefix}{self.errcode.to_str()}"

    def get_err_reason(self, *, append_desc=True, append_detail=False) -> str:
        r"""Return a failure_reason str.

        Format: Ec-aabb-dee[: <err_description>[\n<detail_exception_info>]]
            c: FAILURE_TYPE_1digit
            aa: API_2digits
            bb: MODULE_2digits
            dee: ERRCODE_3digits
        """
        _errdesc = ""
        if append_desc:
            _errdesc = f": {self.errdesc}."

        _detail = ""
        if append_detail:
            _detail = f"\n{self.otaclient_err.__cause__!r}"

        return (
            f"{self._err_prefix}"
            f"{self.failure_type.to_str()}"
            "-"
            f"{self.api.to_str()}{self.module.to_str()}"
            "-"
            f"{self.errcode.to_str()}"
            f"{_errdesc}{_detail}"
        )

    def get_traceback(self, *, splitter="\n") -> str:
        """Format the traceback into a str with splitter as <splitter>."""
        return splitter.join(traceback.format_tb(self.__traceback__))


class OTAUpdateError(OTA_APIError):
    api = OTAAPI.Update


class OTARollbackError(OTA_APIError):
    api = OTAAPI.Rollback


@unique
class OTAErrorCode(Enum):
    E_UNSPECIFIC = 0

    E_NETWORK = 100
    E_OTAMETA_DOWNLOAD_FAILED = 101

    E_OTA_ERR_RECOVERABLE = 200
    E_OTAUPDATE_BUSY = 201
    E_INVALID_STATUS_FOR_OTAUPDATE = 202
    E_INVALID_OTAUPDATE_REQUEST = 203
    E_INVALID_STATUS_FOR_OTAROLLBACK = 204
    E_OTAMETA_VERIFICATION_FAILED = 205
    E_UPDATEDELTA_GENERATION_FAILED = 206
    E_APPLY_OTAUPDATE_FAILED = 207
    E_BASE_OTAMETA_VERIFICATION_FAILED = 208

    E_OTA_ERR_UNRECOVERABLE = 300
    E_BOOTCONTROL_PLATFORM_UNSUPPORTED = 301
    E_BOOTCONTROL_INIT_ERR = 302
    E_BOOTCONTROL_PREUPDATE_FAILED = 303
    E_BOOTCONTROL_POSTUPDATE_FAILED = 304
    E_BOOTCONTROL_POSTROLLBACK_FAILED = 305
    E_STANDBY_SLOT_SPACE_NOT_ENOUGH_ERROR = 306
    E_BOOTCONTROL_PREROLLBACK_FAILED = 307

    def to_str(self) -> str:
        return f"{self.value:0>3}"

    def get_errcode(self) -> int:
        return self.value

    def get_errname(self) -> str:
        return self.name


###### error exception classes ######
_NETWORK_ERR_DEFAULT_DESC = (
    "error related to network connection detected, "
    "please check the Internet connection and try again"
)


### network error ###
class NetworkError(OTAError):
    """Generic network error"""

    failure_type: wrapper.FailureType = wrapper.FailureType.RECOVERABLE
    module: OTAModules = OTAModules.Downloader
    errcode: OTAErrorCode = OTAErrorCode.E_NETWORK
    desc: str = _NETWORK_ERR_DEFAULT_DESC


class OTAMetaDownloadFailed(NetworkError):
    errcode: OTAErrorCode = OTAErrorCode.E_OTAMETA_DOWNLOAD_FAILED
    desc: str = f"{_NETWORK_ERR_DEFAULT_DESC}: failed to download ota image meta"


### recoverable error ###
_RECOVERABLE_DEFAULT_DESC = (
    "recoverable ota error(unrelated to network) detected, "
    "please resend the request or retry after a restart of ota-client/ECU"
)


class OTAErrorRecoverable(OTAError):
    failure_type: wrapper.FailureType = wrapper.FailureType.RECOVERABLE
    # followings are default values
    module: OTAModules = OTAModules.General
    errcode: OTAErrorCode = OTAErrorCode.E_OTA_ERR_RECOVERABLE
    desc: str = _RECOVERABLE_DEFAULT_DESC


class OTAUpdateBusy(OTAErrorRecoverable):
    module: OTAModules = OTAModules.API
    errcode: OTAErrorCode = OTAErrorCode.E_OTAUPDATE_BUSY
    desc: str = f"{_RECOVERABLE_DEFAULT_DESC}: on-going ota update detected, this request has been ignored"


class OTARollBusy(OTAErrorRecoverable):
    module: OTAModules = OTAModules.API
    errcode: OTAErrorCode = OTAErrorCode.E_OTAUPDATE_BUSY
    desc: str = f"{_RECOVERABLE_DEFAULT_DESC}: on-going ota update detected, this request has been ignored"


class InvalidStatusForOTAUpdate(OTAErrorRecoverable):
    module: OTAModules = OTAModules.API
    errcode: OTAErrorCode = OTAErrorCode.E_INVALID_STATUS_FOR_OTAUPDATE
    desc: str = f"{_RECOVERABLE_DEFAULT_DESC}: current ota-status indicates it should not accept ota update"


class InvalidUpdateRequest(OTAErrorRecoverable):
    module: OTAModules = OTAModules.API
    errcode: OTAErrorCode = OTAErrorCode.E_INVALID_OTAUPDATE_REQUEST
    desc: str = (
        f"{_RECOVERABLE_DEFAULT_DESC}: incoming ota update request's content is invalid"
    )


class InvalidStatusForOTARollback(OTAErrorRecoverable):
    module: OTAModules = OTAModules.API
    errcode: OTAErrorCode = OTAErrorCode.E_INVALID_STATUS_FOR_OTAROLLBACK
    desc: str = f"{_RECOVERABLE_DEFAULT_DESC}: current ota-status indicates it should not accept ota rollback"


class OTAMetaVerificationFailed(OTAErrorRecoverable):
    module: OTAModules = OTAModules.StandbySlotCreater
    errcode: OTAErrorCode = OTAErrorCode.E_OTAMETA_VERIFICATION_FAILED
    desc: str = (
        f"{_RECOVERABLE_DEFAULT_DESC}: hash verification failed for ota meta files"
    )


class UpdateDeltaGenerationFailed(OTAErrorRecoverable):
    module: OTAModules = OTAModules.StandbySlotCreater
    errcode: OTAErrorCode = OTAErrorCode.E_UPDATEDELTA_GENERATION_FAILED
    desc: str = f"{_RECOVERABLE_DEFAULT_DESC}: (rebuild_mode) failed to calculate and/or prepare update delta"


class ApplyOTAUpdateFailed(OTAErrorRecoverable):
    module: OTAModules = OTAModules.StandbySlotCreater
    errcode: OTAErrorCode = OTAErrorCode.E_APPLY_OTAUPDATE_FAILED
    desc: str = f"{_RECOVERABLE_DEFAULT_DESC}: failed to apply ota update"


class BaseOTAMetaVerificationFailed(OTAErrorRecoverable):
    module: OTAModules = OTAModules.API
    errcode: OTAErrorCode = OTAErrorCode.E_BASE_OTAMETA_VERIFICATION_FAILED
    desc: str = f"{_RECOVERABLE_DEFAULT_DESC}: verification failed for base otameta"


### unrecoverable error ###
_UNRECOVERABLE_DEFAULT_DESC = (
    "unrecoverable ota error detected, please contact technical support"
)


class OTAErrorUnRecoverable(OTAError):
    failure_type: wrapper.FailureType = wrapper.FailureType.RECOVERABLE
    module: OTAModules = OTAModules.General
    errcode: OTAErrorCode = OTAErrorCode.E_OTA_ERR_UNRECOVERABLE
    desc: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: unspecific unrecoverable ota error, please contact technical support"


class BootControlPlatformUnsupported(OTAErrorUnRecoverable):
    module: OTAModules = OTAModules.BootController
    errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_PLATFORM_UNSUPPORTED
    desc: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: current ECU platform is not supported by the boot controller module"


class BootControlInitError(OTAErrorUnRecoverable):
    module: OTAModules = OTAModules.BootController
    errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_INIT_ERR
    desc: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: failed to init boot controller module"


class BootControlPreUpdateFailed(OTAErrorUnRecoverable):
    module: OTAModules = OTAModules.BootController
    errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_PREUPDATE_FAILED
    desc: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: pre_update process failed"


class BootControlPostUpdateFailed(OTAErrorUnRecoverable):
    module: OTAModules = OTAModules.BootController
    errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_POSTUPDATE_FAILED
    desc: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: post_update process failed, switch boot is not finished"


class BootControlPostRollbackFailed(OTAErrorUnRecoverable):
    module: OTAModules = OTAModules.BootController
    errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_POSTUPDATE_FAILED
    desc: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: post_rollback process failed, switch boot is not finished"


class StandbySlotSpaceNotEnoughError(OTAErrorUnRecoverable):
    module: OTAModules = OTAModules.StandbySlotCreater
    errcode: OTAErrorCode = OTAErrorCode.E_STANDBY_SLOT_SPACE_NOT_ENOUGH_ERROR
    desc: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: standby slot has insufficient space to apply update, abort"


class BootControlPreRollbackFailed(OTAErrorUnRecoverable):
    module: OTAModules = OTAModules.BootController
    errcode: OTAErrorCode = OTAErrorCode.E_BOOTCONTROL_PREROLLBACK_FAILED
    desc: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: pre_rollback process failed"
