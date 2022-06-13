from enum import Enum, unique


class OtaError(Exception):
    ...


class OtaErrorBusy(OtaError):
    ...


class OtaErrorRecoverable(OtaError):
    ...


class OtaErrorUnrecoverable(OtaError):
    ...


@unique
class OTAOperationFailureType(Enum):
    NO_FAILURE = 0
    RECOVERABLE = 1
    UNRECOVERABLE = 2
