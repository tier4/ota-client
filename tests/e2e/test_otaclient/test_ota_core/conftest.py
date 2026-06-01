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
"""Local fixtures/constants for ota_core e2e tests."""

from __future__ import annotations

# NOTE: for loading autouse fixtures
from tests._fixtures_ota_core import (  # noqa: F401
    mock_certs_dir,
    mock_ensure_umount,
    mock_fstrim,
)

# Dummy CloudFront cookies
COOKIES_JSON = (
    '{"CloudFront-Policy": "dummy_policy", "CloudFront-Signature": "dummy_signature",'
    '"CloudFront-Key-Pair-Id": "dummy_key_pair_id"}'
)
