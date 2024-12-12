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

import random
from sqlite3 import Connection
from typing import Any, Callable, ClassVar, Generator, Literal, Optional

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
    schema_ver: ClassVar[Literal[1]] = 1

    digest: Annotated[
        bytes,
        TypeAffinityRepr(bytes),
        ConstrainRepr("PRIMARY KEY"),
        SkipValidation,
    ]
    """sha256 digest of the original file."""

    path: Annotated[
        Optional[str],
        TypeAffinityRepr(str),
        SkipValidation,
    ] = None
    """NOTE: only for resource without zstd compression."""

    original_size: Annotated[
        int,
        TypeAffinityRepr(int),
        ConstrainRepr("NOT NULL"),
        SkipValidation,
    ]
    """The size of the plain uncompressed resource."""

    compression_alg: Annotated[
        Optional[str],
        TypeAffinityRepr(str),
        SkipValidation,
    ] = None
    """The compression algorthim used to compressed the resource.

    NOTE that this field should be None if <contents> is not None.
    """

    def __eq__(self, other: Any | Self) -> bool:
        return isinstance(other, self.__class__) and self.digest == other.digest

    def __hash__(self) -> int:
        return hash(self.digest)


class ResourceTableORM(ORMBase[ResourceTable]):

    table_name: ClassVar[Literal["resource_table"]] = "resource_table"

    def __init__(
        self,
        con: Connection,
        schema_name: str | None | Literal["temp"] = None,
    ) -> None:
        super().__init__(con, self.table_name, schema_name)

    def iter_all_with_shuffle(self, *, batch_size: int) -> Generator[Self, None, None]:
        """Iter all entries with seek method by rowid, shuffle each batch before yield.

        NOTE: the target table must has rowid defined!
        """
        _this_batch = []
        for _entry in self.orm_select_all_with_pagination(batch_size=batch_size):
            _this_batch.append(_entry)
            if len(_this_batch) >= batch_size:
                random.shuffle(_this_batch)
                yield from _this_batch
                _this_batch = []
        random.shuffle(_this_batch)
        yield from _this_batch


class RSTableORMThreadPool(ORMThreadPoolBase[ResourceTable]):

    table_name: ClassVar[Literal["resource_table"]] = "resource_table"

    def __init__(
        self,
        schema_name: str | None = None,
        *,
        con_factory: Callable[[], Connection],
        number_of_cons: int,
        thread_name_prefix: str = "",
    ) -> None:
        super().__init__(
            self.table_name,
            schema_name,
            con_factory=con_factory,
            number_of_cons=number_of_cons,
            thread_name_prefix=thread_name_prefix,
        )
