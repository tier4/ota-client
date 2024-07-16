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
from typing import Optional

from pydantic import SkipValidation
from simple_sqlite3_orm import ConstrainRepr, TableSpec, TypeAffinityRepr
from simple_sqlite3_orm._orm import AsyncORMThreadPoolBase
from typing_extensions import Annotated

from ._consts import HEADER_CONTENT_ENCODING, HEADER_OTA_FILE_CACHE_CONTROL
from .cache_control import OTAFileCacheControl
from .config import config as cfg

logger = logging.getLogger(__name__)


class CacheMetaTable(TableSpec):
    """revision 4

    url: unquoted URL from the request of this cache entry.
    bucket_id: the LRU bucket this cache entry in.
    last_access: the UNIX timestamp of last access.
    cache_size: the file size of cached file(not the size of corresponding OTA file!).
    file_sha256:
        a. string of the sha256 hash of original OTA file(uncompressed),
        b. if a. is not available, then it is in form of "URL_<sha256_of_URL>".
    file_compression_alg: the compression used for the cached OTA file entry,
        if no compression is applied, then empty.
    content_encoding: the content_encoding header string comes with resp from remote server.
    """

    file_sha256: Annotated[
        bytes,
        TypeAffinityRepr(bytes),
        ConstrainRepr("PRIMARY KEY"),
        SkipValidation,
    ]
    url: Annotated[
        str,
        TypeAffinityRepr(str),
        ConstrainRepr("NOT NULL"),
        SkipValidation,
    ]
    bucket_idx: Annotated[
        int,
        TypeAffinityRepr(int),
        ConstrainRepr("NOT NULL"),
        SkipValidation,
    ]
    last_access: Annotated[
        int,
        TypeAffinityRepr(int),
        ConstrainRepr("NOT NULL"),
        SkipValidation,
    ]
    cache_size: Annotated[
        int,
        TypeAffinityRepr(int),
        ConstrainRepr("NOT NULL"),
        SkipValidation,
    ]
    file_compression_alg: Annotated[
        Optional[str],
        TypeAffinityRepr(str),
        ConstrainRepr(("DEFAULT", "NULL")),
        SkipValidation,
    ] = None
    content_encoding: Annotated[
        Optional[str],
        TypeAffinityRepr(str),
        ConstrainRepr(("DEFAULT", "NULL")),
        SkipValidation,
    ] = None

    def export_headers_to_client(self) -> dict[str, str]:
        """Export required headers for client.

        Currently includes content-type, content-encoding and ota-file-cache-control headers.
        """
        res = {}
        if self.content_encoding:
            res[HEADER_CONTENT_ENCODING] = self.content_encoding

        # export ota-file-cache-control headers if file_sha256 is valid file hash
        if self.file_sha256.startswith(cfg.URL_BASED_HASH_PREFIX):
            res[HEADER_OTA_FILE_CACHE_CONTROL] = (
                OTAFileCacheControl.export_kwargs_as_header(
                    file_sha256=self.file_sha256.hex(),
                    file_compression_alg=self.file_compression_alg or "",
                )
            )
        return res


class AsyncCacheMetaORM(AsyncORMThreadPoolBase[CacheMetaTable]): ...
