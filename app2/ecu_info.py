import yaml


class EcuInfo:
    def __init__(self):
        ecu_info_path = "/boot/ota/ecu_info.yaml"
        self._ecu_info = self._load_ecu_info(ecu_info_path)

    def get_secondary_ecus():
        return self._ecu_info["secondaries"]

    def get_ecu_id():
        return self._ecu_info["ecu_id"]

    def _load_ecu_info(self):
        with open(self._ecu_info_path) as f:
            ecu_info = yaml.load(f, Loader=yaml.SafeLoader)
            format_version = ecu_info["format_version"]
            if format_version != 1:
                raise ValueError(f"format_version={format_version} is illegal")
            return ecu_info
