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
from enum import Enum
from typing import Set


class OTAFileCacheControl(Enum):
    """Custom header for ota file caching control policies.

    format:
        Ota-File-Cache-Control: <directive>
    directives:
        retry_cache: indicates that ota_proxy should clear cache entry for <URL>
            and retry caching
        no_cache: indicates that ota_proxy should not use cache for <URL>
        use_cache: implicitly applied default value, conflicts with no_cache directive
            no need(and no effect) to add this directive into the list

    NOTE: using retry_cache and no_cache together will not work as expected,
        only no_cache will be respected, already cached file will not be deleted as retry_cache indicates.
    """

    use_cache = "use_cache"
    no_cache = "no_cache"
    retry_caching = "retry_caching"

    header = "Ota-File-Cache-Control"
    header_lower = "ota-file-cache-control"

    @classmethod
    def parse_to_value_set(cls, input: str) -> Set[str]:
        return set(input.split(","))

    @classmethod
    def parse_to_enum_set(cls, input: str) -> Set["OTAFileCacheControl"]:
        _policies_set = cls.parse_to_value_set(input)
        res = set()
        for p in _policies_set:
            res.add(OTAFileCacheControl[p])

        return res

    @classmethod
    def add_to(cls, target: str, input: "OTAFileCacheControl") -> str:
        _policies_set = cls.parse_to_value_set(target)
        _policies_set.add(input.value)
        return ",".join(_policies_set)
