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

import os
import shutil
import stat
from pathlib import Path
from threading import Lock
from typing import ClassVar, Literal, Optional

from pydantic import SkipValidation
from simple_sqlite3_orm import ConstrainRepr, TableSpec, TypeAffinityRepr
from typing_extensions import Annotated

from ._types import InodeTable, Xattr

CANONICAL_ROOT = "/"


class HardlinkRegister:
    def __init__(self) -> None:
        self._lock = Lock()
        self._inode_first_path_map: dict[int, str] = {}

    def get_first_copy_fpath(self, inode: int, fpath: str) -> tuple[bool, str]:
        """For a specific hardlink group, return the first copy of this group.

        Returns:
            A tuple of bool and str. bool indicates whether the caller is the first copy,
                str is the fpath of the first copy in the group.
        """
        with self._lock:
            first_copy = self._inode_first_path_map.setdefault(inode, fpath)
            return first_copy == fpath, first_copy


class FileSystemTable(TableSpec):
    schema_ver: ClassVar[Literal[1]] = 1
    table_name: ClassVar[Literal["file_table"]] = "file_table"

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

    def _set_xattr(self, _target: Path) -> None:
        """NOTE: this method always not following symlink."""
        if xattrs := self.xattrs:
            for k, v in xattrs.items():
                os.setxattr(
                    path=_target,
                    attribute=k,
                    value=v.encode(),
                    follow_symlinks=False,
                )

    def copy_to_target(
        self,
        target_mnt: Path,
        *,
        resource_dir: Path,
        ctx: HardlinkRegister,
    ) -> None:
        _canonical_path = Path(self.path)
        _relative_path = target_mnt / _canonical_path.relative_to(CANONICAL_ROOT)

        inode_table = self.inode
        _mode = inode_table.mode
        _uid, _gid = inode_table.uid, inode_table.gid

        if stat.S_ISDIR(_mode):
            _relative_path.mkdir(exist_ok=True, parents=True)
            os.chmod(_relative_path, mode=_mode, follow_symlinks=False)
            os.chown(_relative_path, uid=_uid, gid=_gid, follow_symlinks=False)
            self._set_xattr(_relative_path)

        elif stat.S_ISREG(_mode) and self.digest:
            _resource = resource_dir / self.digest.hex()

            # hardlinked file, identify hardlink group by original inode number
            if inode := inode_table.inode:
                _first_one, _first_copy = ctx.get_first_copy_fpath(inode, self.path)

                if _first_one:
                    shutil.copy(_resource, _relative_path)
                    os.chmod(_relative_path, mode=_mode, follow_symlinks=False)
                    os.chown(_relative_path, uid=_uid, gid=_gid, follow_symlinks=False)
                    self._set_xattr(_relative_path)
                else:
                    os.link(_first_copy, _relative_path)
            # normal regular file with nlink==1
            else:
                shutil.copy(_resource, _relative_path)
                os.chmod(_relative_path, mode=_mode, follow_symlinks=False)
                os.chown(_relative_path, uid=_uid, gid=_gid, follow_symlinks=False)
                self._set_xattr(_relative_path)

        elif stat.S_ISLNK(_mode) and self.contents:
            _symlink_target = self.contents.decode()
            _relative_path.symlink_to(_symlink_target)
            # NOTE: changing mode of symlink is not needed and uneffective
            os.chown(_relative_path, uid=_uid, gid=_gid, follow_symlinks=False)
            self._set_xattr(_relative_path)

        elif stat.S_ISCHR(_mode):
            # NOTE(20241206): although we have the major/minor recorded in contents field,
            #   we always force to only use 0,0 as device num, which is used as whiteout
            #   file for the overlayfs.
            _device_num = os.makedev(0, 0)
            os.mknod(_relative_path, mode=_mode, device=_device_num)
            os.chmod(_relative_path, mode=_mode, follow_symlinks=False)
            os.chmod(_relative_path, mode=_mode, follow_symlinks=False)
            self._set_xattr(_relative_path)

        else:
            raise ValueError(f"invalid entry: {self}")
