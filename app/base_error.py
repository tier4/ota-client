import traceback
from enum import Enum, auto, unique

from app.errors import OTAErrorCode


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
    API = 4

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


class OTAError(Exception):
    """Errors that happen during otaclient code executing.

    This exception class should be the base module level exception for each module.
    It should always be captured by the OTAError at otaclient.py.
    """

    failure_type: OTAFailureType
    module: OTAModules
    errcode: OTAErrorCode
    desc: str = "no description available for this error"


class OTA_APIError(Exception):
    """Errors that happen during processing API request.

    This exception class should be the top level exception for each API entry.
    This exception must be created by wrapping an OTAClientError.
    """

    api: OTA_API
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
