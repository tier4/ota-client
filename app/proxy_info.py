import yaml
from pathlib import Path

from configs import config as cfg
import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)

"""
proxy_info.yaml:
# version 1
gateway: bool
enable_ota_proxy: bool
upper_ota_proxy: str
generic_proxy: str
local_server: Tuple[str, int]
"""

DEFUALT_PROXY_INFO = """
gateway: false
enable_ota_proxy: false
"""

class ProxyInfo:

    def __init__(self, proxy_info_file: str=cfg.PROXY_INFO_FILE):
        proxy_info_file_path = Path(proxy_info_file)
        if proxy_info_file_path.is_file():
            proxy_info: dict = yaml.safe_load(proxy_info_file_path.read_text())
        else:
            proxy_info: dict = yaml.safe_load(DEFUALT_PROXY_INFO)

        self.enable_local_ota_proxy: bool = proxy_info.get("enable_ota_proxy", False)
        self.upper_ota_proxy: str = proxy_info.get("upper_ota_proxy")
        self.generic_proxy: str = proxy_info.get("generic_proxy")

        if self.enable_local_ota_proxy:
            self.gateway: bool = proxy_info.get("gateway", False)
            self.host, self.port = proxy_info.get("local_server", ("0.0.0.0", 8000))
    
    def get_proxy_for_local_ota(self) -> str:
        if self.enable_local_ota_proxy:
            # if local proxy is enabled, local ota client also uses it
            return f"http://{self.host}:{self.port}"
        elif self.upper_ota_proxy:
            # else we directly use the upper proxy
            return self.upper_ota_proxy
        elif self.generic_proxy:
            # else we try to use the generic proxy
            return self.generic_proxy
        else:
            # default not using proxy
            return ""

    def get_generic_proxy(self) -> str:
        if self.generic_proxy:
            return self.generic_proxy
        else:
            return ""

proxy_cfg = ProxyInfo()