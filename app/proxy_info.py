import yaml

from configs import config as cfg
import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)

"""
proxy_info.yaml:
# version 1
gateway: <1|0>
enable_ota_proxy: <1|0>
upper_proxy: ""
host: ""
port: ""
"""

class ProxyInfo:

    def __init__(self, proxy_info_file: str=cfg.PROXY_INFO_FILE):
        with open(proxy_info_file, 'r') as f:
            proxy_info = yaml.safe_load(f)

        self.enable_local_ota_proxy: bool = proxy_info.get("enable_ota_proxy", 0) ==  1
        self.upper_proxy: str = proxy_info.get("upper_proxy")

        if self.enable_local_ota_proxy:
            self.gateway: bool = proxy_info.get("gateway", 0) == 1
            self.host: str = proxy_info.get("host", "0.0.0.0")
            self.port: int = proxy_info.get("port", 8000)
    
    def get_proxy_for_local_ota(self) -> str:
        if self.enable_local_ota_proxy:
            # if local proxy is enabled, local ota client also uses it
            return f"http://{self.host}:{self.port}"
        elif self.upper_proxy:
            # else we directly use the upper proxy
            return self.upper_proxy
        else:
            # else we do not use any proxy
            return

proxy_cfg = ProxyInfo()