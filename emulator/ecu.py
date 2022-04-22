from ota_status import OtaStatus

OTA_STATUS_DICT = {status.name: status for status in OtaStatus}


class Ecu:
    def __init__(self, is_main, name, status, version):
        self.is_main = is_main
        self.name = name
        self.status = OTA_STATUS_DICT[status]
        self.version = version

    def set_status(self, status: str):
        self.status = OTA_STATUS_DICT[status]
