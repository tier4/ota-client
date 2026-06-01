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
"""Local re-export for OTA image v1 metadata e2e tests.

Container dependency:
    The v1 OTA image fixture (`/ota-image_v1`) and its CA chain
    (`/certs_ota-image_v1`) are baked into the test container image by
    `docker/test_base/Dockerfile` (copied from the upstream
    `ota_img_for_test` image). The shared `ota_image_v1_server` fixture and the
    v1 container path constants live in the top-level `tests/conftest.py`.
"""

from __future__ import annotations

from tests.conftest import CERTS_OTA_IMAGE_V1_DIR  # noqa: F401
