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
"""Local fixtures and constants for OTA image v1 metadata e2e tests.

Container dependency:
    The v1 OTA image fixture (`/ota-image_v1`) and its CA chain
    (`/certs_ota-image_v1`) are baked into the test container image by
    `docker/test_base/Dockerfile` (copied from the upstream
    `ota_img_for_test` image). Tests in this subtree consume them via
    those absolute container paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Generator

import pytest

from tests.conftest import launch_http_server_subprocess

# Baked into the test container image; see docker/test_base/Dockerfile.
OTA_IMAGE_V1_DIR = Path("/ota-image_v1")
CERTS_OTA_IMAGE_V1_DIR = Path("/certs_ota-image_v1")

OTA_IMAGE_V1_SERVER_ADDR = "127.0.0.1"
OTA_IMAGE_V1_SERVER_PORT = 8081


@pytest.fixture(scope="session")
def ota_image_v1_server() -> Generator[str]:
    """Serve `/ota-image_v1` over HTTP on the v1 port and yield the base URL."""
    with launch_http_server_subprocess(
        OTA_IMAGE_V1_SERVER_ADDR, OTA_IMAGE_V1_SERVER_PORT, OTA_IMAGE_V1_DIR
    ) as base_url:
        yield base_url
