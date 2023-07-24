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


import pytest
from otaclient.ota_proxy import OTAFileCacheControl


@pytest.mark.parametrize(
    "raw_str, expected",
    (
        ("no_cache", OTAFileCacheControl(no_cache=True)),
        ("retry_caching", OTAFileCacheControl(retry_caching=True)),
        (
            "no_cache, file_sha256=sha256value, file_compression_alg=zst",
            OTAFileCacheControl(
                no_cache=True,
                file_sha256="sha256value",
                file_compression_alg="zst",
            ),
        ),
    ),
)
def test_cache_control_header(raw_str, expected):
    assert OTAFileCacheControl.parse_header(raw_str) == expected
