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
import pytest_mock

from otaclient import ota_core
from tests.conftest import TestConfiguration as cfg

OTA_UPDATER_MODULE = ota_core._updater.__name__


@pytest.fixture(autouse=True, scope="module")
def mock_certs_dir(module_mocker: pytest_mock.MockerFixture):
    """Mock to use the certs from the OTA test base image."""
    from otaclient.configs.cfg import cfg as _cfg

    module_mocker.patch.object(
        _cfg,
        "CERT_DPATH",
        cfg.CERTS_DIR,
    )


@pytest.fixture(autouse=True, scope="module")
def module_scope_mock(module_mocker: pytest_mock.MockerFixture):
    module_mocker.patch(
        f"{OTA_UPDATER_MODULE}.fstrim_at_subprocess",
        module_mocker.MagicMock(),
    )
