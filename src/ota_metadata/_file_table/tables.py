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

from typing import Optional

from pydantic import SkipValidation
from simple_sqlite3_orm import ConstrainRepr, TableSpec, TypeAffinityRepr
from typing_extensions import Annotated


class DirectoryTable(TableSpec):
    path: Annotated[
        str,
        TypeAffinityRepr(str),
        ConstrainRepr("PRIMARY KEY"),
        SkipValidation,
    ]
    mode: Annotated[
        int,
        TypeAffinityRepr(int),
        ConstrainRepr("NOT NULL"),
        SkipValidation,
    ]
    uid: Annotated[
        int,
        TypeAffinityRepr(int),
        ConstrainRepr("NOT NULL"),
        SkipValidation,
    ]
    gid: Annotated[
        int,
        TypeAffinityRepr(int),
        ConstrainRepr("NOT NULL"),
        SkipValidation,
    ]


class SymlinkTable(TableSpec):
    path: Annotated[
        str,
        TypeAffinityRepr(str),
        ConstrainRepr("PRIMARY KEY"),
        SkipValidation,
    ]
    uid: Annotated[
        int,
        TypeAffinityRepr(int),
        ConstrainRepr("NOT NULL"),
        SkipValidation,
    ]
    gid: Annotated[
        int,
        TypeAffinityRepr(int),
        ConstrainRepr("NOT NULL"),
        SkipValidation,
    ]
    target: Annotated[
        str,
        TypeAffinityRepr(str),
        ConstrainRepr("NOT NULL"),
        SkipValidation,
    ]


class RegularFileTable(TableSpec):
    path: Annotated[
        str,
        TypeAffinityRepr(str),
        ConstrainRepr("PRIMARY KEY"),
        SkipValidation,
    ]
    mode: Annotated[
        int,
        TypeAffinityRepr(int),
        ConstrainRepr("NOT NULL"),
        SkipValidation,
    ]
    uid: Annotated[
        int,
        TypeAffinityRepr(int),
        ConstrainRepr("NOT NULL"),
        SkipValidation,
    ]
    gid: Annotated[
        int,
        TypeAffinityRepr(int),
        ConstrainRepr("NOT NULL"),
        SkipValidation,
    ]
    size: Annotated[
        int,
        TypeAffinityRepr(int),
        ConstrainRepr("NOT NULL"),
        SkipValidation,
    ]

    # schema: <algh> || ":" || <digest>
    digest: Annotated[
        Optional[bytes],
        TypeAffinityRepr(bytes),
        SkipValidation,
    ] = None

    nlink: Annotated[
        Optional[int],
        TypeAffinityRepr(int),
        SkipValidation,
    ] = None
    inode: Annotated[
        Optional[int],
        TypeAffinityRepr(int),
        SkipValidation,
    ] = None

    contents: Annotated[
        Optional[bytes],
        TypeAffinityRepr(bytes),
        SkipValidation,
    ] = None
