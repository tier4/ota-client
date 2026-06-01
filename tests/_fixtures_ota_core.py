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
"""Shared autouse mocks for the ota_core / updater test subtrees.

These fixtures are deliberately **not** defined in the top-level
``tests/conftest.py``: they are autouse, and applying them globally would
patch ota_core for every test in the suite (including unrelated unit tests)
and force an ota_core import on each one. Instead, the ota_core conftests
(``unit``/``integration``/``e2e``) import the subset they need from here, so
the autouse scope stays confined to those subtrees:

    from tests._fixtures_ota_core import (  # noqa: F401
        mock_certs_dir,
        mock_ensure_umount,
        mock_fstrim,
    )

`_updater.ensure_umount` / `_main.ensure_mount` shell out to real OS mount
syscalls, so any test driving the updater state machine must mock them. The
CA cert dir is repointed at `/certs` (baked into the test container image) so
code consulting `cfg.CERT_DPATH` works without the production install path.
`fstrim_at_subprocess` is silenced so nothing tries to fstrim during tests.
"""

from __future__ import annotations

import pytest
import pytest_mock

from tests.conftest import OTA_UPDATER_MODULE

# Path to certs baked into the test docker image (see docker/test_base/Dockerfile).
_CONTAINER_CERTS_DIR = "/certs"


@pytest.fixture(autouse=True)
def mock_ensure_umount(mocker: pytest_mock.MockerFixture) -> None:
    mocker.patch(f"{OTA_UPDATER_MODULE}.ensure_umount")


@pytest.fixture(autouse=True)
def mock_ensure_mount(mocker: pytest_mock.MockerFixture) -> None:
    mocker.patch("otaclient.ota_core._main.ensure_mount")


@pytest.fixture(autouse=True, scope="module")
def mock_certs_dir(module_mocker: pytest_mock.MockerFixture) -> None:
    """Repoint CERT_DPATH at the certs baked into the test container image."""
    from otaclient.configs.cfg import cfg as _cfg

    module_mocker.patch.object(_cfg, "CERT_DPATH", _CONTAINER_CERTS_DIR)


@pytest.fixture(autouse=True, scope="module")
def mock_fstrim(module_mocker: pytest_mock.MockerFixture) -> None:
    """Block real `fstrim` invocations from leaking out during slot apply."""
    module_mocker.patch(
        f"{OTA_UPDATER_MODULE}.fstrim_at_subprocess",
        module_mocker.MagicMock(),
    )
