class OtaError(Exception):
    ...


class OtaErrorBusy(OtaError):
    ...


class OtaErrorRecoverable(OtaError):
    ...


class OtaErrorUnrecoverable(OtaError):
    ...
