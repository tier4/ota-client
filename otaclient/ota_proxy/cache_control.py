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


from dataclasses import dataclass
from enum import Enum
from typing import Mapping, List
from typing_extensions import Self


class DIRECTIVE(str, Enum):
    no_cache = "no_cache"
    retry_caching = "retry_caching"
    file_sha256 = "file_sha256"
    file_compression_alg = "file_compression_alg"


@dataclass
class CacheControlPolicy:
    no_cache: bool = False
    retry_caching: bool = False
    # added in revision 2:
    file_sha256: str = ""
    file_compression_alg: str = ""

    def export_header_str(self) -> str:
        """Export cache_control policy as ota-file-cache-control header.

        Only set/True policy will be exported, empty or False policy will be skipped.
        """
        _directives: List[str] = []
        for key in DIRECTIVE:
            value = getattr(self, key)
            # key only field
            if isinstance(value, bool) and value:
                _directives.append(key)
            # key_value pair field(ignore empty field)
            elif value:
                _directives.append(f"{key}={value}")
        return OTAFileCacheControl.SEPARATOR.join(_directives)

    def update_from_header_str(self, _input: str) -> Self:
        if not _input:
            return self

        for _raw_directive in _input.split(OTAFileCacheControl.SEPARATOR):
            _parsed = _raw_directive.strip().split("=", maxsplit=1)
            if len(_parsed) < 1:
                continue

            try:
                key = DIRECTIVE[_parsed[0]]
            except KeyError:
                continue

            if len(_parsed) == 1:
                setattr(self, key, True)
            elif len(_parsed) == 2 and (_value := _parsed[1]):
                setattr(self, key, _value.strip())
        return self


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

    # NOTE: according to RFC7230, the header name is case-insensitive,
    #       so for convenience during code implementation, we always use lower-case
    #       header name.
    HEADER_LOWERCASE = "ota-file-cache-control"
    SEPARATOR = ","

    @classmethod
    def parse_header(cls, _input: str) -> CacheControlPolicy:
        return CacheControlPolicy().update_from_header_str(_input)

    @classmethod
    def export_as_header(cls, **kwargs: Mapping[DIRECTIVE, str]) -> str:
        """
        Only set/True policy will be exported, empty or False policy will be skipped.
        """
        _directives: List[str] = []
        for k, v in kwargs:
            try:
                k = DIRECTIVE[k]
            except KeyError:
                continue

            if isinstance(v, bool) and v:
                _directives.append(k)
            elif v:
                _directives.append(f"{k}={v}")
        return cls.SEPARATOR.join(_directives)
