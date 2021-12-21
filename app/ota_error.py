class OtaErrorBusy(Exception):
    pass


class OtaErrorRecoverable(Exception):
    pass


class OtaErrorUnrecoverable(Exception):
    pass


class OtaErrorCancel(Exception):
    def __init__(self):
        super().__init__("canceled")
