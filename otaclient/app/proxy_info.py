# Copyright 2022 TIER IV, INC. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""Proxy setting parsing.

check docs/README.md for more details.
"""
import yaml
import warnings
from dataclasses import dataclass, fields
from typing import Any, ClassVar, Dict
from pathlib import Path

from . import log_setting
from .configs import config as cfg
from .configs import server_cfg

logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)

DEFAULT_MAIN_ECU_PROXY_INFO = """
enable_local_ota_proxy: true
gateway: true
"""


@dataclass
class ProxyInfo:
    """OTA-proxy configuration.

    NOTE(20221216): for mainECU, if proxy_info.yaml is not presented,
                    a default _DEFAULT_MAIN_ECU_PROXY_INFO will be used!
    Attributes:
        enable_local_ota_proxy: whether to launch a local ota_proxy server.
        enable_local_ota_proxy_cache: enable cache mechanism on ota-proxy.
        gateway: whether to use HTTPS when local ota_proxy connects to remote.
        local_ota_proxy_listen_addr: ipaddr ota_proxy listens on.
        local_ota_proxy_listen_port: port ota_proxy used.
        upper_ota_proxy: the upper proxy used by local ota_proxy(proxy chain).
    """

    # NOTE(20221219): the default values for the following settings
    #                 now align with v2.5.4
    gateway: bool = False
    upper_ota_proxy: str = ""
    enable_local_ota_proxy: bool = False
    local_ota_proxy_listen_addr: str = server_cfg.OTA_PROXY_LISTEN_ADDRESS
    local_ota_proxy_listen_port: int = server_cfg.OTA_PROXY_LISTEN_PORT
    # NOTE: this field not presented in v2.5.4,
    #       for current implementation, it should be default to True.
    #       This field doesn't take effect if enable_local_ota_proxy is False
    enable_local_ota_proxy_cache: bool = True

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
            f"failed to load {proxy_info_file=} or config file corrupted, "
            "use default main ECU config"
        )
        _loaded = yaml.safe_load(DEFAULT_MAIN_ECU_PROXY_INFO)

    # load options
    # NOTE: if option is not presented,
    # this option will be set to the default value
    _proxy_info_dict: Dict[str, Any] = dict()
    for _field in fields(ProxyInfo):
        _option = _loaded.get(_field.name)
        if not isinstance(_option, _field.type):
            if _option is not None:
                logger.warning(
                    f"{_field.name} contains invalid value={_option}, "
                    f"ignored and set to default={_field.default}"
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
