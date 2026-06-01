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
"""Local constants for legacy2 metadata e2e tests.

Container dependency:
    The legacy OTA image fixture (`/ota-image`) and its CA chain (`/certs`)
    are baked into the test container image by `docker/test_base/Dockerfile`
    (copied from the upstream `ota_img_for_test` image). The shared
    `legacy_ota_image_server` fixture and the `OTA_IMAGE_DIR` / `CERTS_DIR`
    constants live in the top-level `tests/conftest.py`; only the
    legacy-specific signing artifacts are local.
"""

from __future__ import annotations

from tests.conftest import CERTS_DIR, OTA_IMAGE_DIR  # noqa: F401

OTA_IMAGE_SIGN_CERT = OTA_IMAGE_DIR / "sign.pem"
METADATA_JWT = OTA_IMAGE_DIR / "metadata.jwt"
