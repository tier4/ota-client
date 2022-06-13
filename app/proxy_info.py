import yaml
import warnings
from dataclasses import dataclass, fields
from typing import Any, ClassVar, Dict
from pathlib import Path

from app import log_util
from app.configs import config as cfg
from app.configs import server_cfg

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
enable_local_ota_proxy: true
gateway: true
enable_local_ota_proxy_cache: true
"""

##                        internal ecu                    ##
# for internal ecu that doesn't have direct internet connection,
# the following is the template configuration for it.
"""
# internal ecu is not the gateway for the local network.
gateway: false

# set to false if internal ECU doesn't have child ECU to serve.
enable_local_ota_proxy: false

# typically, we can only enable ota cache on the gateway ECU.
enable_local_ota_proxy_cache: false

# for internal ECU, upper_ota_proxy is required, 
# internal ECU will use this proxy to request for ota update.
# upper ota proxy must be an HTTP URL.
upper_ota_proxy: <upper_ota_proxy_URL: str>

# the listen_addr of local_ota_proxy, if not presented, default to 0.0.0.0:8082
local_ota_proxy_listen_addr: "0.0.0.0"
local_ota_proxy_listen_port: 8082
"""
######


@dataclass
class ProxyInfo:
    """OTA-proxy configuration.

    NOTE: all the default values are for mainECU!
    Attributes:
        enable_local_ota_proxy: whether to launch a local ota_proxy server, default is True.
        gateway: (only valid when enable_local_ota_proxy==true) whether to enforce HTTPS when local ota_proxy
            sends out the requests, default is True.
        enable_local_ota_proxy_cache: enable cache mechanism on ota-proxy, default is True.
        local_ota_proxy_listen_addr: default is "0.0.0.0".
        local_ota_proxy_listen_port: default is 8082.
        upper_ota_proxy: the upper proxy used by local ota_proxy(proxy chain), default is None(no upper proxy).
    """

    enable_local_ota_proxy: bool = True
    gateway: bool = True
    # to be compatible with mainECU
    enable_local_ota_proxy_cache: bool = True
    upper_ota_proxy: str = None
    local_ota_proxy_listen_addr: str = server_cfg.OTA_PROXY_LISTEN_ADDRESS
    local_ota_proxy_listen_port: int = server_cfg.OTA_PROXY_LISTEN_PORT

    # for maintaining compatibility, will be removed in the future
    # Dict[str, str]: <old_option_name> -> <new_option_name>
    # 20220526: "enable_ota_proxy" -> "enable_local_ota_proxy"
    _compatibility: ClassVar[Dict[str, str]] = {
        "enable_ota_proxy": "enable_local_ota_proxy"
    }

    def get_proxy_for_local_ota(self) -> str:
        if self.enable_local_ota_proxy:
            # if local proxy is enabled, local ota client also uses it
            return f"http://{self.local_ota_proxy_listen_addr}:{self.local_ota_proxy_listen_port}"
        elif self.upper_ota_proxy:
            # else we directly use the upper proxy
            return self.upper_ota_proxy
        else:
            # default not using proxy
            return ""


def parse_proxy_info(proxy_info_file: str = cfg.PROXY_INFO_FILE) -> ProxyInfo:
    _loaded: Dict[str, Any]
    try:
        _loaded = yaml.safe_load(Path(proxy_info_file).read_text())
        assert isinstance(_loaded, Dict)
    except (FileNotFoundError, AssertionError):
        logger.warning(
            f"failed to load {proxy_info_file=} or config file corrupted, use default config"
        )
        _loaded = dict()

    # load options
    # NOTE: if option is not presented,
    # this option will be set to the default value
    _proxy_info_dict: Dict[str, Any] = dict()
    for _field in fields(ProxyInfo):
        _option = _loaded.get(_field.name)
        if not isinstance(_option, _field.type):
            if _option is not None:
                logger.warning(
                    f"{_field.name} contains invalid value={_option}, ignored and set to default={_field.default}"
                )
            _proxy_info_dict[_field.name] = _field.default
            continue

        _proxy_info_dict[_field.name] = _option

    # maintain compatiblity with old proxy_info format
    for old, new in ProxyInfo._compatibility.items():
        if old in _loaded:
            warnings.warn(
                f"option field '{old}' is replaced by '{new}', "
                f"and the support for '{old}' option might be dropped in the future. "
                f"please use '{new}' instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            _proxy_info_dict[new] = _loaded[old]

    return ProxyInfo(**_proxy_info_dict)


proxy_cfg = parse_proxy_info()

if __name__ == "__main__":
    proxy_cfg = parse_proxy_info("./proxy_info.yaml")
    logger.info(f"{proxy_cfg!r}, {proxy_cfg.get_proxy_for_local_ota()=}")
