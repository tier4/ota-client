"""OTA error code definition"""
from enum import Enum, unique
from typing import Dict


@unique
class OTAErrorCode(Enum):
    E_UNSPECIFIC = 0

    E_NETWORK = 100

    E_OTA_ERR_RECOVERABLE = 200

    E_OTA_ERR_UNRECOVERABLE = 300

    def to_str(self) -> str:
        return f"{self.value:0>3}"

    def get_errcode(self) -> int:
        return self.value

    def get_errname(self) -> str:
        return self.name

    def get_errdesc(self) -> str:
        try:
            return ErrorDescription[self]
        except KeyError:
            return ErrorDescription[self.E_UNSPECIFIC]


ErrorDescription: Dict["OTAErrorCode", str] = {
    OTAErrorCode.E_UNSPECIFIC: "unspecific ota error",
    OTAErrorCode.E_NETWORK: (
        "error related to network connection detected, "
        "please check the Internet connection and try again"
    ),
    OTAErrorCode.E_OTA_ERR_RECOVERABLE: (
        "recoverable ota error(unrelated to network connection) detected, "
        "please restart ota-client or ECU and try again"
    ),
    OTAErrorCode.E_OTA_ERR_UNRECOVERABLE: (
        "unrecoverable ota error detected, please contact technical support"
    ),
}
