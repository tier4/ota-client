import yaml
from pathlib import Path

from configs import config as cfg
from configs import server_cfg
import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)

# Example proxy_info.yaml(for subecu)
"""
# gateway: false
enable_ota_proxy: true
local_server: ["0.0.0.0", 8082]
upper_ota_proxy: "http://10.0.0.2:8082"
"""


###### Example proxy_info.yaml for different scenario ######
##                       mainecu                          ##
# for mainecu, proxy_info.yaml is not needed, the following
# DEFAULT_PROXY_INFO will be used.
# we should treat the ecu with no proxy_info.yaml
# as main ecu(as gateway), and enable ota_proxy without upper proxy.
DEFUALT_PROXY_INFO = """
enable_ota_proxy: true
gateway: true
"""

##                        subecu                           ##
"""
# gateway option should not be presented, or set to false
gateway: false
enable_ota_proxy: true
# upper ota proxy must be an HTTP URL
upper_ota_proxy: <upper_ota_proxy_URL: str>
# the listen_addr of local_ota_proxy, 
# if not presented, default to ["0.0.0.0", 8082]
local_server: [<ipaddr: str>, <port: int>]
"""
######


class ProxyInfo:
    """Ota proxy configuration.

    Attributes:
        enable_local_ota_proxy: whether to launch a local ota_proxy server
        host, port: (only valid when enable_local_ota_proxy==true) the listen address of the local ota_proxy
        gateway: (only valid when enable_local_ota_proxy==true) whether to enforce HTTPS when local ota_proxy
            sends out the requests
        upper_ota_proxy: the upper proxy used by local ota_proxy(proxy chain)
    """

    def __init__(self, proxy_info_file: str = cfg.PROXY_INFO_FILE):
        proxy_info_file_path = Path(proxy_info_file)
        if proxy_info_file_path.is_file():
            proxy_info: dict = yaml.safe_load(proxy_info_file_path.read_text())
        else:
            proxy_info: dict = yaml.safe_load(DEFUALT_PROXY_INFO)

        self.enable_local_ota_proxy: bool = proxy_info.get("enable_ota_proxy", False)
        self.upper_ota_proxy: str = proxy_info.get("upper_ota_proxy")

        if self.enable_local_ota_proxy:
            self.gateway: bool = proxy_info.get("gateway", False)
            self.host, self.port = proxy_info.get(
                "local_server", server_cfg.OTA_PROXY_SERVER_ADDR
            )

    def get_proxy_for_local_ota(self) -> str:
        if self.enable_local_ota_proxy:
            # if local proxy is enabled, local ota client also uses it
            return f"http://{self.host}:{self.port}"
        elif self.upper_ota_proxy:
            # else we directly use the upper proxy
            return self.upper_ota_proxy
        else:
            # default not using proxy
            return ""


proxy_cfg = ProxyInfo()
