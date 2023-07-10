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
from typing import List, Optional
from typing_extensions import Self


@dataclass
class CacheControlPolicy:
    no_cache: bool = False
    retry_caching: bool = False
    # added in revision 2:
    file_sha256: str = ""
    file_compression_alg: str = ""

    def to_header_str(self) -> str:
        """Export cache_control policy as ota-file-cache-control header.

        Only set/True policy will be exported, empty or False policy will be skipped.
        """
        _directives: List[str] = []
        for field in fields(self):
            _key, _value = field.name, getattr(self, field.name)
            # key only field
            if field.type is bool and _value:
                _directives.append(field.name)
            # key_value pair field(ignore empty field)
            elif _value:
                _directives.append(f"{_key}={_value}")
        return OTAFileCacheControl.SEPARATOR.join(_directives)


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

    # NOTE: according to RFC7230, the header name is case-insensitive,
    #       so for convenience during code implementation, we always use lower-case
    #       header name.
    HEADER_LOWERCASE = "ota-file-cache-control"
    SEPARATOR = ","

    @classmethod
    def parse_header(cls, _input: str) -> CacheControlPolicy:
        res = CacheControlPolicy()
        for _raw_directive in _input.split(cls.SEPARATOR):
            _parsed = _raw_directive.strip().split("=", maxsplit=1)
            # key only field, set to True on presented
            if len(_parsed) == 1 and (
                _directive := cls.DIRECTIVE.check_directive(_parsed[0])
            ):
                setattr(res, _directive.strip(), True)
            # kv field
            elif len(_parsed) == 2 and (
                _directive := cls.DIRECTIVE.check_directive(_parsed[0])
            ):
                _value = _parsed[1].strip()
                setattr(res, _directive.strip(), _value)
        return res
