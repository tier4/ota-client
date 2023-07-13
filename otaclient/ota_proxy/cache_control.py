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
from typing import List, Optional, Union
from typing_extensions import Self


class DIRECTIVE(str, Enum):
    no_cache = "no_cache"
    retry_caching = "retry_caching"
    file_sha256 = "file_sha256"
    file_compression_alg = "file_compression_alg"

    @classmethod
    def check_key(cls, _input: str) -> Optional[Self]:
        try:
            return cls[_input]
        except KeyError:
            pass


@dataclass
class CacheControlPolicy:
    no_cache: bool = False
    retry_caching: bool = False
    # added in revision 2:
    file_sha256: str = ""
    file_compression_alg: str = ""

    def update_from_header_str(self, _input: str) -> Self:
        """Update itself from raw ota-file-cache-control header string."""
        if not _input:
            return self

        for _raw_directive in _input.split(OTAFileCacheControl.SEPARATOR):
            if not (_parsed := _raw_directive.split("=", maxsplit=1)):
                continue

            key = _parsed[0].strip()
            if not DIRECTIVE.check_key(key):
                continue

            if len(_parsed) == 1:
                setattr(self, key, True)
            elif len(_parsed) == 2 and (value := _parsed[1]):
                setattr(self, key, value.strip())
        return self

    def update_from_directives(self, **kwargs: Union[str, bool]) -> Self:
        """Update itself from a list of directives pairs."""
        for key, value in kwargs.items():
            if DIRECTIVE.check_key(key):
                setattr(self, key, value)
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
    def export_as_header(cls, **kwargs) -> str:
        """Directly export header str from a list of directive pairs.

        Check DIRECTIVE for directives definition.
        Only set/True policy will be exported, empty or False policy will be skipped.
        """
        _directives: List[str] = []
        for key, value in kwargs.items():
            if not DIRECTIVE.check_key(key):
                continue

            if isinstance(value, bool) and value:
                _directives.append(key)
            elif value:
                _directives.append(f"{key}={value}")
        return cls.SEPARATOR.join(_directives)

    @classmethod
    def update_header_str(cls, _input: str, **kwargs) -> str:
        """Update input header string with input directive pairs.

        Current used directives:
        1. no_cache
        2. retry_caching
        3. file_sha256
        4. file_compression_alg
        """
        _parsed_directives = {}
        for _raw_directive in _input.split(OTAFileCacheControl.SEPARATOR):
            if not (_parsed := _raw_directive.split("=", maxsplit=1)):
                continue

            key = _parsed[0].strip()
            if not DIRECTIVE.check_key(key):
                continue
            _parsed_directives[key] = _raw_directive

        for _key, value in kwargs.items():
            if not DIRECTIVE.check_key(_key):
                continue

            if isinstance(value, str):
                _parsed_directives[_key] = f"{_key}={value}"
            elif value:
                _parsed_directives[_key] = _key
            else:  # remove False or empty directives
                _parsed_directives.pop(_key, None)
        return cls.SEPARATOR.join(_parsed_directives.values())
