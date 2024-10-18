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

import os.path as os_path
from typing import ClassVar, Optional
from urllib.parse import quote

from simple_sqlite3_orm import ConstrainRepr, ORMBase, TableSpec, TypeAffinityRepr
from typing_extensions import Annotated

from otaclient_common.common import urljoin_ensure_base

BATCH_SIZE = 128
DIGEST_ALG = b"sha256"

# legacy OTA image specific resource table


class ResourceTable(TableSpec):
    sha256digest: Annotated[
        str,
        TypeAffinityRepr(str),
        ConstrainRepr("PRIMARY KEY"),
    ]
    compression_alg: Annotated[Optional[str], TypeAffinityRepr(str)] = None

    # NOTE: for backward compatible with revision 1
    path: Annotated[
        Optional[str],
        TypeAffinityRepr(str),
    ] = None

    size: Annotated[int, TypeAffinityRepr(int), ConstrainRepr("NOT NULL")]

    SUPPORTED_COMPRESSION_ALG: ClassVar[set[str]] = {"zst", "zstd"}

    def get_download_url(self, url_base: str) -> str:
        data_url = urljoin_ensure_base(url_base, "data")
        data_zst_url = urljoin_ensure_base(url_base, "data.zst")

        # v2 OTA image, with compression enabled
        # example: http://example.com/base_url/data.zstd/a665a45920422f9d417e4867efdc4fb8a04a1f3fff1fa07e998e86f7f7a27ae3.<compression_alg>
        if (
            compression_alg := self.compression_alg
        ) and compression_alg in self.SUPPORTED_COMPRESSION_ALG:
            return urljoin_ensure_base(
                data_zst_url,
                # NOTE: hex alpha-digits and dot(.) are not special character
                #       so no need to use quote here.
                f"{self.sha256digest}.{compression_alg}",
            )

        # v1 OTA image, uncompressed and use full path as URL path
        # example: http://example.com/base_url/data/rootfs/full/path/file
        if not self.path:
            raise ValueError("for uncompressed file, file path is required")
        return urljoin_ensure_base(
            data_url,
            quote(str(os_path.relpath(self.path, "/"))),
        )


class ResourceTableORM(ORMBase[ResourceTable]): ...
