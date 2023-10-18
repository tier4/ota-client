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
from typing import Dict, List, ClassVar
from typing_extensions import Self

from otaclient._utils.typing import copy_callable_typehint_to_method


_FIELDS = "_fields"


@dataclass
class _HeaderDef:
    # ------ Header definition ------ #
    # NOTE: according to RFC7230, the header name is case-insensitive,
    #       so for convenience during code implementation, we always use lower-case
    #       header name.
    HEADER_LOWERCASE: ClassVar[str] = "ota-file-cache-control"
    HEADER_DIR_SEPARATOR: ClassVar[str] = ","

    # ------ Directives definition ------ #
    no_cache: bool = False
    retry_caching: bool = False
    # added in revision 2:
    file_sha256: str = ""
    file_compression_alg: str = ""

    def __init_subclass__(cls) -> None:
        _fields = {}
        for f in fields(cls):
            _fields[f.name] = f.type
        setattr(cls, _FIELDS, _fields)


@dataclass
class OTAFileCacheControl(_HeaderDef):
    """Custom header for ota file caching control policies.

    format:
        Ota-File-Cache-Control: <directive or directive_kv_pair>[, <directive or directive_kv_pair>,[...]]

    directives:
        no_cache: indicates that ota_proxy should not use cache for <URL>,
            this will void all other directives when presented,
        retry_caching: indicates that ota_proxy should clear cache entry for <URL>
            and retry caching when presented,
        file_sha256: the hash value of the original requested OTA file
        file_compression_alg: the compression alg used for the OTA file
    """

    @classmethod
    def parse_header(cls, _input: str) -> Self:
        _fields: Dict[str, type] = getattr(cls, _FIELDS)
        _parsed_directives = {}
        for _raw_directive in _input.split(cls.HEADER_DIR_SEPARATOR):
            if not (_parsed := _raw_directive.strip().split("=", maxsplit=1)):
                continue

            key = _parsed[0].strip()
            if not (_field_type := _fields.get(key)):
                continue

            if _field_type is bool:
                _parsed_directives[key] = True
            elif len(_parsed) == 2 and (value := _parsed[1].strip()):
                _parsed_directives[key] = value
        return cls(**_parsed_directives)

    @classmethod
    @copy_callable_typehint_to_method(_HeaderDef)
    def export_kwargs_as_header(cls, **kwargs) -> str:
        """Directly export header str from a list of directive pairs."""
        _fields: Dict[str, type] = getattr(cls, _FIELDS)
        _directives: List[str] = []
        for key, value in kwargs.items():
            if key not in _fields:
                continue

            if isinstance(value, bool) and value:
                _directives.append(key)
            elif value:  # str field
                _directives.append(f"{key}={value}")
        return cls.HEADER_DIR_SEPARATOR.join(_directives)

    @classmethod
    def update_header_str(cls, _input: str, **kwargs) -> str:
        """Update input header string with input directive pairs.

        Current used directives:
        1. no_cache
        2. retry_caching
        3. file_sha256
        4. file_compression_alg
        """
        _fields: Dict[str, type] = getattr(cls, _FIELDS)
        _parsed_directives = {}
        for _raw_directive in _input.split(cls.HEADER_DIR_SEPARATOR):
            if not (_parsed := _raw_directive.strip().split("=", maxsplit=1)):
                continue
            key = _parsed[0].strip()
            if key not in _fields:
                continue
            _parsed_directives[key] = _raw_directive

        for _key, value in kwargs.items():
            if not (_field_type := _fields.get(_key)):
                continue

            if _field_type is bool and value:
                _parsed_directives[_key] = _key
            elif value:
                _parsed_directives[_key] = f"{_key}={value}"
            else:  # remove False or empty directives
                _parsed_directives.pop(_key, None)
        return cls.HEADER_DIR_SEPARATOR.join(_parsed_directives.values())
