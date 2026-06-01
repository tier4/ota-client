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
"""Local fixtures/constants for ota_core e2e tests.

The shared OTA-image infrastructure (container paths, server constants,
`SlotMeta`, the `legacy_ota_image_server`/`ota_image_v1_server`/`ab_slots`
fixtures) lives in the top-level `tests/conftest.py`; the autouse ota_core
mocks live in `tests/_fixtures_ota_core`. Both are re-exported / imported here
so test modules in this subtree keep importing them via `from .conftest`.
"""

from __future__ import annotations

from tests._fixtures_ota_core import (  # noqa: F401
    mock_certs_dir,
    mock_ensure_umount,
    mock_fstrim,
)
from tests.conftest import (  # noqa: F401
    CERTS_DIR,
    CERTS_OTA_IMAGE_V1_DIR,
    OTA_UPDATER_MODULE,
    UPDATE_VERSION,
    SlotMeta,
)

# Signed CloudFront cookies for the OTA image fixture; the signature is
# expired, but the value still needs to round-trip through the downloader
# JSON parser unchanged.
COOKIES_JSON = (
    '{"CloudFront-Policy": "eyJTdGF0ZW1lbnQiOlt7IlJlc291cmNlIjoiaHR0cHM6Ly9maX'
    "Jtd2FyZS1pbWFnZS5jaS53ZWIuYXV0by94Ml9kZXYvOWZjMTA2ZjAtMWJlZC00YzMyLTg5Zm"
    "EtNjUzOWYxZDc1NWJlLyoiLCJDb25kaXRpb24iOnsiRGF0ZUxlc3NUaGFuIjp7IkFXUzpFcG"
    '9jaFRpbWUiOjE3NTEzMzI0OTN9fX1dfQ__", "CloudFront-Signature": "iGhqJjjLnN'
    "UNuF8zdy0wJVUVXABsIPdCgy0rrnLHXT8MANJtcFydyf0LcxKzbIR9654ek0NmkYgeUakv5U"
    "96pacGWfNgVO0z-5BxZiZjaph9PLFqX0kanmSUGTk2vdQm0o67qg~hiTBh0~OzdXK12J~Uuc"
    "Obr4xgm7TxH08QFbVxRzvSkFVVqNhd2JqFp70ihgS~AGtn8ZmOUsHRNIfqiLkz4HdvqgvnpJ"
    "TmvEyFYeaooSEw1usJ3svbUzhJ3WB25UiShUymGtcG5QHVcApB-jH40hfW8qd42l06OQb6J2"
    'E6XMEw710PczGWeZf3WbV7nmSE-2C5J7pZXZadePXi8w__", "CloudFront-Key-Pair-Id'
    '": "K2HIO3GARJTNVV"}'
)
