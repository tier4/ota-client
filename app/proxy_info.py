from dataclasses import dataclass, fields
from typing import Any, ClassVar, Dict, Type
import yaml
from pathlib import Path

from configs import config as cfg
from configs import server_cfg
import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)

###### Example proxy_info.yaml for different scenario ######
##                       mainecu                          ##
# for mainecu, proxy_info.yaml is not needed, the following
# DEFAULT_PROXY_INFO will be used.
# we should treat the ecu with no proxy_info.yaml
# as main ecu(as gateway), and enable ota_proxy without upper proxy.
"""
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


@dataclass
class ProxyInfo:
    """Ota proxy configuration.

    Attributes:
        enable_ota_proxy: whether to launch a local ota_proxy server
        gateway: (only valid when enable_ota_proxy==true) whether to enforce HTTPS when local ota_proxy
            sends out the requests
        ota_proxy_listen_addr
        ota_proxy_listen_port
        upper_ota_proxy: the upper proxy used by local ota_proxy(proxy chain)
    """

    enable_ota_proxy: bool = True
    gateway: bool = True
    upper_ota_proxy: str = None
    ota_proxy_listen_addr: str = server_cfg.OTA_PROXY_LISTEN_ADDRESS
    ota_proxy_listen_port: int = server_cfg.OTA_PROXY_LISTEN_PORT

    @classmethod
    def parse_proxy_info(
        cls, proxy_info_file: str = cfg.PROXY_INFO_FILE
    ) -> "ProxyInfo":
        proxy_info_file_path = Path(proxy_info_file)
        _proxy_info_dict: Dict[str, Any] = dict()
        if proxy_info_file_path.is_file():
            _proxy_info_dict = yaml.safe_load(proxy_info_file_path.read_text())

        # load options
        # NOTE: if option is not presented,
        # this option will be set to the default value
        for _field in fields(cls):
            _proxy_info_dict.setdefault(_field.name, _field.default)

        return cls(**_proxy_info_dict)

    def get_proxy_for_local_ota(self) -> str:
        if self.enable_ota_proxy:
            # if local proxy is enabled, local ota client also uses it
            return f"http://{self.ota_proxy_listen_addr}:{self.ota_proxy_listen_port}"
        elif self.upper_ota_proxy:
            # else we directly use the upper proxy
            return self.upper_ota_proxy
        else:
            # default not using proxy
            return ""


proxy_cfg = ProxyInfo()

if __name__ == "__main__":
    proxy_cfg = ProxyInfo.parse_proxy_info("./proxy_info.yaml")
    logger.info(f"{proxy_cfg!r}, {proxy_cfg.get_proxy_for_local_ota()=}")
