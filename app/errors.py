"""OTA error code definition"""
from enum import Enum, auto, unique

from app.base_error import OTAError, OTAFailureType, OTAModules


@unique
class OTAErrorCode(Enum):
    E_UNSPECIFIC = 0

    E_NETWORK = 100
    E_OTAMETA_DOWNLOAD_FAILED = auto()

    E_OTA_ERR_RECOVERABLE = 200
    E_OTAUPDATE_BUSY = auto()
    E_INVALID_STATUS_FOR_OTAUPDATE = auto()
    E_INVALID_OTAUPDATE_REQUEST = auto()
    E_INVALID_STATUS_FOR_OTAROLLBACK = auto()
    E_OTAMETA_VERIFICATION_FAILED = auto()
    E_UPDATEDELTA_GENERATION_FAILED = auto()
    E_APPLY_OTAUPDATE_FAILED = auto()

    E_OTA_ERR_UNRECOVERABLE = 300
    E_BOOTCONTROL_INIT_ERR = auto()
    E_BOOTCONTROL_PREUPDATE_FAILED = auto()
    E_BOOTCONTROL_POSTUPDATE_FAILED = auto()
    E_BOOTCONTROL_POSTROLLBACK_FAILED = auto()

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

    failure_type: OTAFailureType = OTAFailureType.RECOVERABLE
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
    failure_type: OTAFailureType = OTAFailureType.RECOVERABLE
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


### unrecoverable error ###
_UNRECOVERABLE_DEFAULT_DESC = (
    "unrecoverable ota error detected, please contact technical support"
)


class OTAErrorUnRecoverable(OTAError):
    failure_type: OTAFailureType = OTAFailureType.RECOVERABLE
    module: OTAModules = OTAModules.General
    errcode: OTAErrorCode = OTAErrorCode.E_OTA_ERR_UNRECOVERABLE
    desc: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: unspecific unrecoverable ota error, please contact technical support"


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
