import yaml

from ota_error import OtaErrorUnrecoverable
import configs as cfg
import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class EcuInfo:
    ECU_INFO_FILE = cfg.ECU_INFO_FILE
    DEFAULT_ECU_INFO = {
        "format_version": 1,  # current version is 1
        "ecu_id": "autoware",  # should be unique for each ECU in vehicle
    }

    def __init__(self):
        ecu_info_file = EcuInfo.ECU_INFO_FILE
        self._ecu_info = self._load_ecu_info(ecu_info_file)

    def get_secondary_ecus(self):
        return self._ecu_info.get("secondaries", [])

    def get_ecu_id(self):
        return self._ecu_info["ecu_id"]

    def _load_ecu_info(self, path):
        try:
            with open(path) as f:
                ecu_info = yaml.load(f, Loader=yaml.SafeLoader)
        except Exception as e:
            return EcuInfo.DEFAULT_ECU_INFO

        format_version = ecu_info.get("format_version")
        if format_version != 1:
            raise OtaErrorUnrecoverable(f"format_version={format_version} is illegal")
        return ecu_info
