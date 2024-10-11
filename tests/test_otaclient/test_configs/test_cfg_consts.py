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

from pytest_mock import MockerFixture

from otaclient.configs import Consts, _cfg_consts


def test_cfg_consts_dynamic_root(mocker: MockerFixture):
    _real_root = "/host_root"

    mocker.patch.object(_cfg_consts, "CANONICAL_ROOT", _real_root)
    mocked_consts = Consts()

    assert mocked_consts.ACTIVE_ROOT == _real_root
    assert mocked_consts.RUN_DIR == "/run/otaclient"
    assert mocked_consts.BOOT_DPATH == "/host_root/boot"
    assert mocked_consts.OTA_API_SERVER_PORT == 50051
