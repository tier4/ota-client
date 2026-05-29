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
"""Shared mocks for ota_core integration tests.

`_updater.ensure_umount` shells out to the real OS umount syscall; the
updater calls it on the session workdir during `execute()` cleanup, so
integration tests that drive the updater state machine must mock it.
The CA cert dir is repointed at `/certs` (baked into the test container
image) so any code path that consults `cfg.CERT_DPATH` works without
relying on the production install path. `fstrim_at_subprocess` is
silenced so nothing tries to fstrim during tests.
"""

from __future__ import annotations

import pytest
import pytest_mock

from otaclient import ota_core

# Path to certs baked into the test docker image (see docker/test_base/Dockerfile).
_CONTAINER_CERTS_DIR = "/certs"

OTA_UPDATER_MODULE = ota_core._updater.__name__


@pytest.fixture(autouse=True)
def mock_ensure_umount(mocker: pytest_mock.MockerFixture) -> None:
    mocker.patch(f"{OTA_UPDATER_MODULE}.ensure_umount")


@pytest.fixture(autouse=True, scope="module")
def mock_certs_dir(module_mocker: pytest_mock.MockerFixture):
    """Repoint CERT_DPATH at the certs baked into the test container image."""
    from otaclient.configs.cfg import cfg as _cfg

    module_mocker.patch.object(_cfg, "CERT_DPATH", _CONTAINER_CERTS_DIR)


@pytest.fixture(autouse=True, scope="module")
def mock_fstrim(module_mocker: pytest_mock.MockerFixture):
    """Block real `fstrim` invocations from leaking out of integration tests."""
    module_mocker.patch(
        f"{OTA_UPDATER_MODULE}.fstrim_at_subprocess",
        module_mocker.MagicMock(),
    )
