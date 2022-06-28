import traceback
from enum import Enum, unique

from app.error_code import OTAErrorCode


@unique
class OTAFailureType(Enum):
    NO_FAILURE = 0
    RECOVERABLE = 1
    UNRECOVERABLE = 2

    def to_str(self) -> str:
        return f"{self.value:0>1}"


@unique
class OTAModules(Enum):
    General = 0
    BootController = 1
    StandbySlotCreater = 2
    Downloader = 3

    def to_str(self) -> str:
        return f"{self.value:0>2}"


@unique
class OTA_API(Enum):
    Unspecific = 0
    Update = 1
    Rollback = 2
    Status = 3

    def to_str(self) -> str:
        return f"{self.value:0>2}"


class OTAClientError(Exception):
    """Errors that happen during otaclient code executing.

    This exception class should be the base module level exception for each module.
    It should always be captured by the OTAError at otaclient.py.
    """

    failure_type: OTAFailureType
    module: OTAModules
    errcode: "OTAErrorCode"


class OTAError(Exception):
    """Errors that happen during processing API request.

    This exception class should be the top level exception for each API entry.
    This exception must be created by wrapping an OTAClientError.
    """

    api: OTA_API
    _err_prefix = "E"

    def __init__(self, otaclient_err: OTAClientError, *args: object) -> None:
        super().__init__(*args)
        self.otaclient_err = otaclient_err
        self.errcode = otaclient_err.errcode
        self.module = otaclient_err.module
        self.failure_type = otaclient_err.failure_type

    def get_errcode(self) -> "OTAErrorCode":
        return self.errcode

    def get_errcode_str(self) -> str:
        return f"{self._err_prefix}{self.errcode.to_str()}"

    def get_err_reason(self, *, append_desc=True) -> str:
        """Return a failure_reason str.

        Format: Ec-aabb-dee[: <err_description>]
            c: FAILURE_TYPE_1digit
            aa: API_2digits
            bb: MODULE_2digits
            dee: ERRCODE_3digits
        """
        _errdesc = ""
        if append_desc:
            _errdesc = f": {self.errcode.get_errdesc()}"

        return (
            f"{self._err_prefix}"
            f"{self.failure_type.to_str()}"
            "-"
            f"{self.api.to_str()}{self.module.to_str()}"
            "-"
            f"{self.errcode.to_str()}"
            f"{_errdesc}"
        )

    def get_traceback(self, *, splitter="\n") -> str:
        """Format the traceback into a str with splitter as <splitter>."""
        return splitter.join(traceback.format_tb(self.__traceback__))
