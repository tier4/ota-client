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

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

import otaclient.configs._proxy_info as _proxy_info_module
from otaclient.configs import ProxyInfo
from otaclient.configs._proxy_info import DEFAULT_PROXY_INFO, parse_proxy_info

logger = logging.getLogger(__name__)

MODULE_NAME = _proxy_info_module.__name__


@pytest.mark.parametrize(
    "_input_yaml, _expected",
    [
        # ------ case 1: proxy_info.yaml is missing ------ #
        # this case is for single ECU that doesn't have proxy_info.yaml and
        # can directly connect to the remote.
        # NOTE: this default value is for x1 backward compatibility.
        pytest.param(
            "# this is an empty file",
            (False, DEFAULT_PROXY_INFO),
            id="empty_yaml_falls_back_to_default",
        ),
        # ------ case 2: typical sub ECU's proxy_info.yaml ------ #
        pytest.param(
            (
                "enable_local_ota_proxy: true\n"
                'upper_ota_proxy: "http://10.0.0.1:8082"\n'
                "enable_local_ota_proxy_cache: true\n"
                'logging_server: "http://10.0.0.1:8083"\n'
                'logging_server_grpc: "http://10.0.0.1:8084"\n'
            ),
            (
                True,
                ProxyInfo(
                    **{
                        "format_version": 1,
                        "upper_ota_proxy": "http://10.0.0.1:8082",
                        "enable_local_ota_proxy": True,
                        "enable_local_ota_proxy_cache": True,
                        "local_ota_proxy_listen_addr": "0.0.0.0",
                        "local_ota_proxy_listen_port": 8082,
                        "logging_server": "http://10.0.0.1:8083",
                        "logging_server_grpc": "http://10.0.0.1:8084",
                    }
                ),
            ),
            id="typical_sub_ecu_proxy_info",
        ),
        # ------ case 3: invalid/corrupted proxy_info.yaml ------ #
        # If the proxy_info.yaml is not a yaml, otaclient will also treat
        # this case the same as proxy_info.yaml missing, the pre-defined
        # proxy_info.yaml will be used.
        pytest.param(
            "not a valid proxy_info.yaml",
            (False, DEFAULT_PROXY_INFO),
            id="invalid_yaml_falls_back_to_default",
        ),
        # ------ case 4: proxy_info.yaml is valid yaml but contains invalid fields ------ #
        # in this case, default predefined default proxy_info.yaml will be loaded
        # NOTE(20240126): Previous behavior is invalid field will be replaced by default value,
        #                   and other fields will be preserved as much as possible.
        #                 This is not proper, now the behavior changes to otaclient will treat
        #                   the whole config file as invalid and load the default config.
        pytest.param(
            (
                "enable_local_ota_proxy: dafef\n"
                "upper_ota_proxy: true\n"
                "enable_local_ota_proxy_cache: adfaea\n"
                "local_ota_proxy_listen_addr: 123\n"
                'local_ota_proxy_listen_port: "2808"\n'
            ),
            (False, DEFAULT_PROXY_INFO),
            id="invalid_field_values_fall_back_to_default",
        ),
        # ------ case 5: corrupted/invalid yaml ------ #
        # in this case, default predefined default proxy_info.yaml will be loaded
        pytest.param(
            "/t/t/t/t/t/t/t/tyaml file should not contain tabs/t/t/t/",
            (False, DEFAULT_PROXY_INFO),
            id="yaml_with_tabs_falls_back_to_default",
        ),
        # ------ case 6: backward compatibility test ------ #
        # for superseded field, the corresponding field should be assigned.
        # for removed field, it should not impact the config file loading.
        pytest.param(
            "enable_ota_proxy: true\ngateway: false\n",
            (True, DEFAULT_PROXY_INFO),
            id="deprecated_field_aliases_to_new",
        ),
        # ------ case 7(20240626): NetworkPort allow str value ------ #
        pytest.param(
            'local_ota_proxy_listen_port: "8082"',
            (True, ProxyInfo(local_ota_proxy_listen_port=8082)),
            id="network_port_accepts_str",
        ),
    ],
)
def test_proxy_info(
    tmp_path: Path,
    _input_yaml: str,
    _expected: tuple[bool, ProxyInfo],
    mocker: MockerFixture,
) -> None:
    # disable deprecation warning on test
    mocker.patch(f"{MODULE_NAME}._deprecation_check")

    proxy_info_file = tmp_path / "proxy_info.yml"
    proxy_info_file.write_text(_input_yaml)
    _res = parse_proxy_info(str(proxy_info_file))

    assert _res == _expected
