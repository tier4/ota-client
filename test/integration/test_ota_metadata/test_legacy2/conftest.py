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
"""Local fixtures and constants for legacy2 metadata integration tests.

Container dependency:
    The legacy v1 OTA image fixture is baked into the test container image
    by `docker/test_base/Dockerfile` (copied from the upstream
    `ota_img_for_test` image). Tests in this subtree consume it via the
    absolute container path `/ota-image`.
"""

from __future__ import annotations

from pathlib import Path

# Absolute container path for the legacy v1 OTA image fixture; baked in by
# `docker/test_base/Dockerfile`. Do not relocate to `test/data/`.
OTA_IMAGE_DIR = Path("/ota-image")
