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
enable_local_ota_proxy_cache: true
"""
EMPTY_PROXY_INFO: str = ""

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
_COMMON_DEFAULT_PARSED_CONFIGS: Dict[str, Any] = {
    "enable_local_ota_proxy": True,
    "enable_local_ota_proxy_cache": True,
    "local_ota_proxy_listen_addr": "0.0.0.0",
    "local_ota_proxy_listen_port": 8082,
}
_DEFAULT_PARSED_MAIN_ECU_CONFIGS: Dict[str, Any] = {
    "enable_local_ota_proxy": True,
    "gateway": True,
    "upper_ota_proxy": "",
}


@pytest.mark.parametrize(
    "_input_yaml, _expected",
    (
        # case 1: testing minimun main ECU proxy_info
        (
            MAINECU_PROXY_INFO,
            {
                **_COMMON_DEFAULT_PARSED_CONFIGS,
                **{
                    "gateway": True,
                    "upper_ota_proxy": "",
                },
            },
        ),
        # case 2: tesing typical sub ECU setting
        (
            PERCEPTION_ECU_PROXY_INFO,
            {
                **_COMMON_DEFAULT_PARSED_CONFIGS,
                **{
                    "gateway": False,
                    "upper_ota_proxy": "http://10.0.0.1:8082",
                },
            },
        ),
        # case 3: testing missing/invalid proxy_info.yaml
        (
            "not a valid proxy_info.yaml",
            {
                **_COMMON_DEFAULT_PARSED_CONFIGS,
                **_DEFAULT_PARSED_MAIN_ECU_CONFIGS,
            },
        ),
        # case 4: testing default settings against invalid fields
        # NOTE: if proxy_info.yaml is valid yaml but fields are invalid,
        #       default value settings will be applied
        # NOTE 2: default value settings are for sub ECU
        (
            CORRUPTED_PROXY_INFO,
            {
                **_COMMON_DEFAULT_PARSED_CONFIGS,
                **{"gateway": False, "upper_ota_proxy": ""},
            },
        ),
    ),
)
def test_proxy_info(tmp_path: Path, _input_yaml: str, _expected: Dict[str, Any]):
    from otaclient.app.proxy_info import parse_proxy_info

    proxy_info_file = tmp_path / "proxy_info.yml"
    proxy_info_file.write_text(_input_yaml)
    _proxy_info = parse_proxy_info(str(proxy_info_file))

    assert asdict(_proxy_info) == _expected
