"""OTA error code definition"""
from enum import Enum, auto, unique
from typing import Dict

from app.base_error import OTAError, OTAFailureType, OTAModules


@unique
class OTAErrorCode(Enum):
    E_UNSPECIFIC = 0

    E_NETWORK = 100

    E_OTA_ERR_RECOVERABLE = 200
    E_OTAUPDATE_BUSY = auto()
    E_INVALID_STATUS_FOR_OTAUPDATE = auto()

    E_OTA_ERR_UNRECOVERABLE = 300

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
    failure_type: OTAFailureType = OTAFailureType.RECOVERABLE
    module: OTAModules = OTAModules.Downloader
    errcode: OTAErrorCode = OTAErrorCode.E_NETWORK
    desc: str = _NETWORK_ERR_DEFAULT_DESC


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


class InvalidStatusForOTAUpdate(OTAErrorRecoverable):
    module: OTAModules = OTAModules.API
    errcode: OTAErrorCode = OTAErrorCode.E_INVALID_STATUS_FOR_OTAUPDATE
    desc: str = f"{_RECOVERABLE_DEFAULT_DESC}: current ota-status indicates it should not accept ota update"


### unrecoverable error ###
_UNRECOVERABLE_DEFAULT_DESC = (
    "unrecoverable ota error detected, please contact technical support"
)


class OTAErrorUnRecoverable(OTAError):
    failure_type: OTAFailureType = OTAFailureType.RECOVERABLE
    module: OTAModules = OTAModules.General
    errcode: OTAErrorCode = OTAErrorCode.E_OTA_ERR_UNRECOVERABLE
    desc: str = f"{_UNRECOVERABLE_DEFAULT_DESC}: unspecific unrecoverable ota error, please contact technical support"
