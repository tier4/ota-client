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
"""proxy_info.yaml definition and parsing logic."""


from __future__ import annotations
import logging
import yaml
import warnings
from functools import cached_property
from typing import Any, ClassVar
from pathlib import Path
from pydantic import AliasChoices, Field

from otaclient._utils.typing import StrOrPath
from otaclient.configs.app_cfg import app_config as cfg
from otaclient.configs._common import (
    BaseFixedConfig,
    IPAddressAny,
    HTTPURLAny,
    NetworkPort,
)

logger = logging.getLogger(__name__)


class ProxyInfo(BaseFixedConfig):
    """OTA-proxy configuration.

    NOTE 1(20221220): when proxy_info.yaml is missing/not a valid yaml,
                      a pre_defined proxy_info.yaml as follow will be used.

    Attributes:
        format_version: the proxy_info.yaml scheme version, current is 1.
        enable_local_ota_proxy: whether to launch a local ota_proxy server.
        enable_local_ota_proxy_cache: enable cache mechanism on ota-proxy.
        local_ota_proxy_listen_addr: ipaddr ota_proxy listens on.
        local_ota_proxy_listen_port: port ota_proxy used.
        upper_ota_proxy: the URL of upper OTA proxy used by local ota_proxy server
            or otaclient(proxy chain).
        logging_server: the URL of AWS IoT otaclient logs upload server.
    """

    format_version: int = 1
    # NOTE(20221219): the default values for the following settings
    #                 now align with v2.5.4
    upper_ota_proxy: HTTPURLAny = Field(default="", validate_default=False)
    enable_local_ota_proxy: bool = Field(
        default=False,
        # NOTE(20240126): "enable_ota_proxy" is superseded by "enable_local_ota_proxy".
        validation_alias=AliasChoices(
            "enable_local_ota_proxy",
            "enable_ota_proxy",
        ),
    )
    local_ota_proxy_listen_addr: IPAddressAny = cfg.OTA_PROXY_LISTEN_ADDRESS
    local_ota_proxy_listen_port: NetworkPort = cfg.OTA_PROXY_LISTEN_PORT
    # NOTE: this field not presented in v2.5.4,
    #       for current implementation, it should be default to True.
    #       This field doesn't take effect if enable_local_ota_proxy is False
    enable_local_ota_proxy_cache: bool = True

    # NOTE(20240201): check ota_client_log_server_port var in autoware_ecu_setup
    #                 ansible configurations.
    LOGGING_SERVER_PORT: ClassVar[int] = 8083
    # NOTE: when logging_server is not configured, it implicitly means the logging server
    #       is located at localhost.
    #       check roles/ota_client/templates/run.sh.j2 in ecu_setup repo.
    logging_server: HTTPURLAny = f"http://127.0.0.1:{LOGGING_SERVER_PORT}"

    def get_proxy_for_local_ota(self) -> str | None:
        """Tell local otaclient which proxy to use(or not use any)."""
        if self.enable_local_ota_proxy:
            # if local otaproxy is enabled, local otaclient also uses it
            return f"http://{self.local_ota_proxy_listen_addr}:{self.local_ota_proxy_listen_port}"
        elif self.upper_ota_proxy:
            # else we directly use the upper proxy
            return self.upper_ota_proxy
        # default not using proxy

    @cached_property
    def gateway_otaproxy(self) -> bool:
        """Whether this local otaproxy is a gateway otaproxy.

        Evidence is if no upper_ota_proxy, then this otaproxy should act as a gateway.
        NOTE(20240202): this replaces the previous user-configurable gateway field in
                        the proxy_info.yaml.
        """
        return not bool(self.upper_ota_proxy)


# deprecated field definition
# <deprecated_old_name> -> <new_field_name>
_deprecated_field: dict[str, str] = {"enable_ota_proxy": "enable_local_ota_proxy"}


def _deprecation_check(_in: dict[str, Any]) -> None:
    """
    NOTE: in the future if pydantic support deprecated field annotated, use that
          mechanism instead of this function.
    """
    for old_fname, _ in _in.items():
        if new_fname := _deprecated_field.get(old_fname):
            warnings.warn(
                f"option field '{old_fname}' is superseded by '{new_fname}', "
                f"and the support for '{old_fname}' option might be dropped in the future. "
                f"please use '{new_fname}' in proxy_info.yaml instead.",
                DeprecationWarning,
                stacklevel=2,
            )


# NOTE: this default is for backward compatible with old device
#       that doesn't have proxy_info.yaml installed.
DEFAULT_PROXY_INFO = ProxyInfo(
    format_version=1,
    enable_local_ota_proxy=True,
)


def parse_proxy_info(proxy_info_file: StrOrPath) -> ProxyInfo:
    try:
        _raw_yaml_str = Path(proxy_info_file).read_text()
    except FileNotFoundError as e:
        logger.warning(f"{proxy_info_file=} not found: {e!r}")
        logger.warning(f"use default proxy_info: {DEFAULT_PROXY_INFO}")
        return DEFAULT_PROXY_INFO

    try:
        loaded_proxy_info = yaml.safe_load(_raw_yaml_str)
        assert isinstance(loaded_proxy_info, dict), "not a valid yaml file"
        _deprecation_check(loaded_proxy_info)
        return ProxyInfo.model_validate(loaded_proxy_info, strict=True)
    except Exception as e:
        logger.warning(f"{proxy_info_file=} is invalid: {e!r}\n{_raw_yaml_str=}")
        logger.warning(f"use default proxy_info: {DEFAULT_PROXY_INFO}")
        return DEFAULT_PROXY_INFO


proxy_info = parse_proxy_info(cfg.PROXY_INFO_FPATH)
