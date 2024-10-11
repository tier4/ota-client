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

import pytest
from pytest import MonkeyPatch

from otaclient.configs import ENV_PREFIX, set_configs


@pytest.mark.parametrize(
    "setting, env_value, value",
    (
        ("DEFAULT_LOG_LEVEL", "CRITICAL", "CRITICAL"),
        (
            "LOG_LEVEL_TABLE",
            r"""{"ota_metadata": "DEBUG"}""",
            {"ota_metadata": "DEBUG"},
        ),
        ("DEBUG_DISABLE_OTAPROXY_HTTPS_VERIFY", "true", True),
        ("DOWNLOAD_INACTIVE_TIMEOUT", "200", 200),
    ),
)
def test_load_configs(setting, env_value, value, monkeypatch: MonkeyPatch):
    monkeypatch.setenv(f"{ENV_PREFIX}{setting}", env_value, value)

    mocked_configs = set_configs()
    assert getattr(mocked_configs, setting) == value


def test_load_default_on_invalid_envs(monkeypatch: MonkeyPatch) -> None:
    # first get a normal configs without any envs
    normal_cfg = set_configs()
    # patch envs
    monkeypatch.setenv(f"{ENV_PREFIX}DOWNLOAD_INACTIVE_TIMEOUT", "not_an_int")
    # check if config is the defualt one
    assert normal_cfg == set_configs()
