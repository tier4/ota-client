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

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import StringIO
from typing import ClassVar, Optional, TypedDict

from typing_extensions import Unpack

from otaclient_common._logging import get_burst_suppressed_logger

logger = logging.getLogger(__name__)
# NOTE: for request_error, only allow max 6 lines of logging per 30 seconds
burst_suppressed_logger = get_burst_suppressed_logger(f"{__name__}.header_parse_error")

VALID_DIRECTORIES = {
    "no_cache",
    "retry_caching",
    "file_sha256",
    "file_compression_alg"
}
HEADER_LOWERCASE = "ota-file-cache-control"
HEADER_DIR_SEPARATOR = ","


class OTAFileCacheDirTypedDict(TypedDict, total=False):
    no_cache: bool
    retry_caching: bool
    # added in revision 2:
    file_sha256: Optional[str]
    file_compression_alg: Optional[str]


def parse_header(_input: str) -> OTAFileCacheControl:
    if not _input:
        return OTAFileCacheControl()

    _res = OTAFileCacheControl()
    for c in _input.strip().split(HEADER_DIR_SEPARATOR):
        k, *v = c.strip().split("=", maxsplit=1)
        if k not in VALID_DIRECTORIES:
            burst_suppressed_logger.warning(f"get unknown directory, ignore: {c}")
            continue
        setattr(_res, k, v[0] if v else True)
    return _res


def _parse_header_asdict(_input: str) -> OTAFileCacheDirTypedDict:
    if not _input:
        return {}

    _res: OTAFileCacheDirTypedDict = {}
    for c in _input.strip().split(HEADER_DIR_SEPARATOR):
        k, *v = c.strip().split("=", maxsplit=1)
        if k not in VALID_DIRECTORIES:
            burst_suppressed_logger.warning(f"get unknown directory, ignore: {c}")
            continue
        _res[k] = v[0] if v else True
    return _res


def export_kwargs_as_header_string(**kwargs: Unpack[OTAFileCacheDirTypedDict]) -> str:
    """Directly export header str from a list of directive pairs."""
    if not kwargs:
        return ""

    with StringIO() as buffer:
        for k, v in kwargs.items():
            if k not in VALID_DIRECTORIES:
                burst_suppressed_logger.warning(f"get unknown directory, ignore: {k}")
                continue
            if not v:
                continue

            buffer.write(k if isinstance(v, bool) and v else f"{k}={v}")
            buffer.write(HEADER_DIR_SEPARATOR)
        return buffer.getvalue().strip(HEADER_DIR_SEPARATOR)


def update_header_str(_input: str, **kwargs: Unpack[OTAFileCacheDirTypedDict]) -> str:
    """Update input header string with input directive pairs."""
    if not kwargs:
        return _input

    _res = _parse_header_asdict(_input)
    _res.update(kwargs)
    return export_kwargs_as_header_string(**_res)


@dataclass
class OTAFileCacheControl:
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

    # ------ Header definition ------ #
    # NOTE: according to RFC7230, the header name is case-insensitive,
    #       so for convenience during code implementation, we always use lower-case
    #       header name.
    HEADER_LOWERCASE: ClassVar[str] = HEADER_LOWERCASE
    HEADER_DIR_SEPARATOR: ClassVar[str] = HEADER_DIR_SEPARATOR

    # ------ Directives definition ------ #
    no_cache: bool = False
    retry_caching: bool = False
    # added in revision 2:
    file_sha256: Optional[str] = None
    file_compression_alg: Optional[str] = None

    # TODO: (20250618): to not change the callers of these methods,
    #                   currently just register these methods under OTAFileCacheControl class.
    parse_header = staticmethod(parse_header)
    export_kwargs_as_header = staticmethod(export_kwargs_as_header_string)
    update_header_str = staticmethod(update_header_str)
