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

from ._types import InodeTable, Xattr


class FileSystemTable(TableSpec):
    path: Annotated[
        str,
        TypeAffinityRepr(str),
        ConstrainRepr("PRIMARY KEY"),
        SkipValidation,
    ]

    inode: Annotated[
        InodeTable,
        TypeAffinityRepr(bytes),
        ConstrainRepr("NOT NULL"),
    ]
    """msgpacked basic attrs from inode table for this file entry.

    Ref: https://www.kernel.org/doc/html/latest/filesystems/ext4/inodes.html

    including the following fields from inode table:
    1. mode bits
    2. uid
    3. gid
    4. inode(when the file is hardlinked)
    """

    xattrs: Annotated[
        Optional[Xattr],
        TypeAffinityRepr(bytes),
    ] = None
    """msgpacked extended attrs for the entry.
    
    It contains a dict of xattr names and xattr values.
    """

    digest: Annotated[
        Optional[bytes],
        TypeAffinityRepr(bytes),
        SkipValidation,
    ] = None
    """If not None, the digest of this regular file."""

    contents: Annotated[
        Optional[bytes],
        TypeAffinityRepr(bytes),
        SkipValidation,
    ] = None
    """The contents of the file.
    
    When is regular, <contents> is the file's contents.
    When is symlink, <contents> is the symlink target.
    When is char, <contents> is a comma separate pair of int,
        which is the the major and minor dev num.
        In most cases, it should be 0,0, which is the whiteout file
        of overlayfs.
    """
