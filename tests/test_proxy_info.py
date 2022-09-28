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


import logging
import pytest
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

MAINECU_PROXY_INFO: str = """
enable_local_ota_proxy: true
gateway: true
"""
PERCEPTION_ECU_PROXY_INFO: str = """
gateway: false
enable_local_ota_proxy: true
upper_ota_proxy: "http://10.0.0.1:8082"
enable_local_ota_proxy_cache: false
"""
EMPTY_PROXY_INFO: str = ""

# define all options as opposite to the default value
FULL_PROXY_INFO: str = """
enable_local_ota_proxy: false
gateway: false
upper_ota_proxy: "http://10.0.0.1:8082"
enable_local_ota_proxy_cache: false
local_ota_proxy_listen_addr: "10.0.0.2"
local_ota_proxy_listen_port: 2808
"""

# corrupted yaml files that contains invalid value
# all fields are asigned with invalid value,
# invalid field should be replaced by default value.
CORRUPTED_PROXY_INFO: str = """
enable_local_ota_proxy: dafef
gateway: 123
upper_ota_proxy: true
enable_local_ota_proxy_cache: adfaea
local_ota_proxy_listen_addr: 123
local_ota_proxy_listen_port: "2808"
"""

# check ProxyInfo for detail
_DEFAULT: Dict[str, Any] = {
    "enable_local_ota_proxy": True,
    "gateway": True,
    "enable_local_ota_proxy_cache": True,
    "upper_ota_proxy": "",
    "local_ota_proxy_listen_addr": "0.0.0.0",
    "local_ota_proxy_listen_port": 8082,
}


@pytest.mark.parametrize(
    "_input_yaml, _expected",
    (
        (
            MAINECU_PROXY_INFO,
            {
                **_DEFAULT,
                **{
                    "enable_local_ota_proxy": True,
                    "gateway": True,
                },
            },
        ),
        (
            PERCEPTION_ECU_PROXY_INFO,
            {
                **_DEFAULT,
                **{
                    "enable_local_ota_proxy": True,
                    "gateway": False,
                    "upper_ota_proxy": "http://10.0.0.1:8082",
                    "enable_local_ota_proxy_cache": False,
                },
            },
        ),
        (EMPTY_PROXY_INFO, _DEFAULT),
        (
            FULL_PROXY_INFO,
            {
                "enable_local_ota_proxy": False,
                "gateway": False,
                "enable_local_ota_proxy_cache": False,
                "upper_ota_proxy": "http://10.0.0.1:8082",
                "local_ota_proxy_listen_addr": "10.0.0.2",
                "local_ota_proxy_listen_port": 2808,
            },
        ),
        (CORRUPTED_PROXY_INFO, _DEFAULT),
    ),
)
def test_proxy_info(tmp_path: Path, _input_yaml: str, _expected: Dict[str, Any]):
    from otaclient.app.proxy_info import parse_proxy_info

    proxy_info_file = tmp_path / "proxy_info.yml"
    proxy_info_file.write_text(_input_yaml)
    _proxy_info = parse_proxy_info(proxy_info_file)

    assert asdict(_proxy_info) == _expected
