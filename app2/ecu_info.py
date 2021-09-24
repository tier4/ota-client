import yaml
from logging import getLogger

import configs as cfg

logger = getLogger(__name__)
logger.setLevel(cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL))


class EcuInfo:
    DEFAULT_ECU_INFO = {
        "format_version": 1,  # current version is 1
        "ecu_id": "autoware",  # should be unique for each ECU in vehicle
    }

    def __init__(self):
        ecu_info_path = "/boot/ota/ecu_info.yaml"
        self._ecu_info = self._load_ecu_info(ecu_info_path)

    def get_secondary_ecus(self):
        return self._ecu_info.get("secondaries", [])

    def get_ecu_id(self):
        return self._ecu_info["ecu_id"]

    def _load_ecu_info(self, path):
        try:
            with open(path) as f:
                ecu_info = yaml.load(f, Loader=yaml.SafeLoader)
                format_version = ecu_info["format_version"]
                if format_version != 1:
                    raise ValueError(f"format_version={format_version} is illegal")
                return ecu_info
        except:
            return EcuInfo.DEFAULT_ECU_INFO
