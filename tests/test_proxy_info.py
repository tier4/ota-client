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
from pathlib import Path
from typing import Any, Dict

from otaclient.configs.proxy_info import parse_proxy_info


logger = logging.getLogger(__name__)

# pre-defined proxy_info.yaml for when
# proxy_info.yaml is missing/not found
PRE_DEFINED_PROXY_INFO_YAML = """\
enable_local_ota_proxy: true
gateway: true
"""
# parsed pre-defined proxy_info.yaml
PARSED_PRE_DEFINED_PROXY_INFO_DICT = {
    "gateway": True,
    "upper_ota_proxy": "",
    "enable_local_ota_proxy": True,
    "enable_local_ota_proxy_cache": True,
    "local_ota_proxy_listen_addr": "0.0.0.0",
    "local_ota_proxy_listen_port": 8082,
    "logging_server": "http://127.0.0.1:8083",
}

PERCEPTION_ECU_PROXY_INFO_YAML = """\
gateway: false
enable_local_ota_proxy: true
upper_ota_proxy: "http://10.0.0.1:8082"
enable_local_ota_proxy_cache: true
"""

# Bad configured yaml file that contains invalid value.
# This yaml file is valid, but all fields' values are invalid.
BAD_CONFIGURED_PROXY_INFO_YAML = """\
enable_local_ota_proxy: dafef
gateway: 123
upper_ota_proxy: true
enable_local_ota_proxy_cache: adfaea
local_ota_proxy_listen_addr: 123
local_ota_proxy_listen_port: "2808"
"""

# Not a yaml file
BAD_CORRUPTED_PROXY_INFO_YAML = """\
/t/t/t/t/t/t/t/tyaml file should not contain tabs/t/t/t/\
"""


@pytest.mark.parametrize(
    "_input_yaml, _expected",
    (
        # case 1: testing when proxy_info.yaml is missing
        # this case is for single ECU that doesn't have proxy_info.yaml and
        # can directly connect to the remote
        (
            PRE_DEFINED_PROXY_INFO_YAML,
            PARSED_PRE_DEFINED_PROXY_INFO_DICT,
        ),
        # case 2: tesing typical sub ECU setting
        (
            PERCEPTION_ECU_PROXY_INFO_YAML,
            {
                "gateway": False,
                "upper_ota_proxy": "http://10.0.0.1:8082",
                "enable_local_ota_proxy": True,
                "enable_local_ota_proxy_cache": True,
                "local_ota_proxy_listen_addr": "0.0.0.0",
                "local_ota_proxy_listen_port": 8082,
                "logging_server": "http://127.0.0.1:8083",
            },
        ),
        # case 3: testing invalid/corrupted proxy_info.yaml
        # If the proxy_info.yaml is not a yaml, otaclient will also treat
        # this case the same as proxy_info.yaml missing, the pre-defined
        # proxy_info.yaml will be used.
        (
            "not a valid proxy_info.yaml",
            PARSED_PRE_DEFINED_PROXY_INFO_DICT,
        ),
        # case 4: testing when proxy_info.yaml is valid yaml but contains invalid fields
        # in this case, default predefined default proxy_info.yaml will be loaded
        # NOTE(20240126): Previous behavior is invalid field will be replaced by default value,
        #                   and other fields will be preserved as much as possible.
        #                 This is not proper, now the behavior changes to otaclient will treat
        #                   the whole config file as invalid and load the default config.
        (
            BAD_CONFIGURED_PROXY_INFO_YAML,
            PARSED_PRE_DEFINED_PROXY_INFO_DICT,
        ),
        # case 5: testing corrupted proxy_info
        # in this case, default predefined default proxy_info.yaml will be loaded
        (
            BAD_CORRUPTED_PROXY_INFO_YAML,
            PARSED_PRE_DEFINED_PROXY_INFO_DICT,
        ),
    ),
)
def test_proxy_info(tmp_path: Path, _input_yaml: str, _expected: Dict[str, Any]):
    proxy_info_file = tmp_path / "proxy_info.yml"
    proxy_info_file.write_text(_input_yaml)
    _proxy_info = parse_proxy_info(str(proxy_info_file))

    assert _proxy_info.model_dump() == _expected
