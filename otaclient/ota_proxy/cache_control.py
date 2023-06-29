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


from dataclasses import dataclass, fields
from enum import Enum
from typing import Dict, List, Optional
from typing_extensions import Self


@dataclass
class CacheControlPolicy:
    no_cache: bool = False
    retry_caching: bool = False
    file_sha256: str = ""
    file_compression_alg: str = ""


class OTAFileCacheControl:
    """Custom header for ota file caching control policies.

    format:
        Ota-File-Cache-Control: <directive or directive_kv_pair>[, <directive or directive_kv_pair>,[...]]

    directives:
        no_cache: indicates that ota_proxy should not use cache for <URL>,
            this will void all other directives when presented,
        retry_caching: indicates that ota_proxy should clear cache entry for <URL>
            and retry caching
        file_sha256: the hash value of the original requested OTA file
        file_compression_alg: the compression alg used for the OTA file
    """

    class DIRECTIVE(str, Enum):
        no_cache = "no_cache"
        retry_caching = "retry_caching"
        file_sha256 = "file_sha256"
        file_compression_alg = "file_compression_alg"

        @classmethod
        def check_directive(cls, _input: str) -> Optional[Self]:
            try:
                return cls(_input)
            except ValueError:
                return

    HEADER = "Ota-File-Cache-Control"
    HEADER_LOWER = "ota-file-cache-control"
    SEPARATOR = ","

    # pre-defined helper header dict
    _NO_CACHE_HEADER = {HEADER_LOWER: DIRECTIVE.no_cache.value}

    @classmethod
    def no_cache_header(cls) -> Dict[str, str]:
        """Helper method that produce a header dict contains no_cache directive."""
        return cls._NO_CACHE_HEADER.copy()

    @classmethod
    def parse_header(cls, _input: str) -> CacheControlPolicy:
        res = CacheControlPolicy()
        for _raw_directive in _input.split(cls.SEPARATOR):
            _parsed = _raw_directive.strip().split("=", maxsplit=1)
            # key only field, set to True on presented
            if len(_parsed) == 1 and (
                _directive := cls.DIRECTIVE.check_directive(_parsed[0])
            ):
                setattr(res, _directive, True)
            # kv field
            elif len(_parsed) == 2 and (
                _directive := cls.DIRECTIVE.check_directive(_parsed[0])
            ):
                setattr(res, _directive, _parsed[1])
        return res

    @classmethod
    def to_header_str(cls, cache_control_policy: CacheControlPolicy) -> str:
        _directives: List[str] = []
        for field in fields(cache_control_policy):
            _key, _value = field.name, getattr(cache_control_policy, field.name)
            # key only field
            if field.type is bool:
                if _value:
                    _directives.append(field.name)
            # key_value pair field
            else:
                _directives.append(f"{_key}={_value}")
        return cls.SEPARATOR.join(_directives)

    @classmethod
    def merge_policy(cls, cache_control_policy: CacheControlPolicy, header: str):
        """Merge raw policy str to an CacheControlPolicy instance."""
        input_policy = cls.parse_header(header)
        for field in fields(CacheControlPolicy):
            setattr(cache_control_policy, field.name, getattr(input_policy, field.name))
