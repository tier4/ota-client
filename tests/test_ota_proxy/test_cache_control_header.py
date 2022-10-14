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
from otaclient.ota_proxy.cache_control import OTAFileCacheControl


@pytest.mark.parametrize(
    "raw_str, expected",
    (
        ("use_cache", {OTAFileCacheControl.use_cache}),
        (
            "use_cache,retry_caching",
            {OTAFileCacheControl.use_cache, OTAFileCacheControl.retry_caching},
        ),
        ("no_cache", {OTAFileCacheControl.no_cache}),
    ),
)
def test_cache_control_header(raw_str, expected):
    assert OTAFileCacheControl.parse_to_enum_set(raw_str) == expected
