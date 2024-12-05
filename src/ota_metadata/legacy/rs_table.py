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
"""Resource table implementation for legacy OTA image."""


from __future__ import annotations

from typing import Any, Optional

from pydantic import SkipValidation
from simple_sqlite3_orm import (
    ConstrainRepr,
    ORMBase,
    TableSpec,
    TypeAffinityRepr,
)
from simple_sqlite3_orm._orm import ORMThreadPoolBase
from typing_extensions import Annotated, Self


class ResourceTable(TableSpec):
    digest: Annotated[
        Optional[bytes],
        TypeAffinityRepr(bytes),
        ConstrainRepr("PRIMARY KEY"),
        SkipValidation,
    ] = None
    """sha256 digest of the original file."""

    path: Annotated[
        Optional[str],
        TypeAffinityRepr(str),
        SkipValidation,
    ] = None
    """NOTE: only for resource without zstd compression."""

    compression_alg: Annotated[
        Optional[str],
        TypeAffinityRepr(str),
        SkipValidation,
    ] = None

    def __eq__(self, other: Any | Self) -> bool:
        return isinstance(other, self.__class__) and self.digest == other.digest

    def __hash__(self) -> int:
        return hash(self.digest)


class ResourceTableORM(ORMBase[ResourceTable]): ...


class RSTableORMThreadPool(ORMThreadPoolBase[ResourceTable]): ...
