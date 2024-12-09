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
from typing import ClassVar, Literal, Optional

from pydantic import BaseModel, SkipValidation
from simple_sqlite3_orm import ConstrainRepr, TableSpec, TypeAffinityRepr
from typing_extensions import Annotated

from otaclient_common.typing import StrOrPath

from ._types import InodeTable, Xattr

CANONICAL_ROOT = "/"

PrepareMethod = Literal["move", "hardlink", "copy"]


class FileTableBase(BaseModel):
    schema_ver: ClassVar[Literal[1]] = 1

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

    contents: Annotated[
        Optional[bytes],
        TypeAffinityRepr(bytes),
        SkipValidation,
    ] = None
    """The contents of the file. Currently only used by symlink.
    
    When is symlink, <contents> is the symlink target.
    """

    def _set_xattr(self, _target: StrOrPath) -> None:
        """NOTE: this method always don't follow symlink."""
        if xattrs := self.xattrs:
            for k, v in xattrs.items():
                os.setxattr(
                    path=_target,
                    attribute=k,
                    value=v.encode(),
                    follow_symlinks=False,
                )

    def _set_perm(self, _target: StrOrPath) -> None:
        """NOTE: this method always don't follow symlink."""
        inode_table = self.inode
        # NOTE: changing mode of symlink is not needed and uneffective, and on some platform
        #   changing mode of symlink will even result in exception raised.
        if not stat.S_ISLNK(inode_table.mode):
            os.chmod(_target, mode=inode_table.mode, follow_symlinks=False)
        os.chown(
            _target, uid=inode_table.uid, gid=inode_table.gid, follow_symlinks=False
        )


class FileTableRegularFiles(TableSpec, FileTableBase):
    """DB table for regular file entries."""

    table_name: ClassVar[str] = "ft_regular"

    digest: Annotated[
        bytes,
        TypeAffinityRepr(bytes),
        SkipValidation,
    ]

    def prepare_target(
        self, _rs: StrOrPath, *, target_mnt: StrOrPath, prepare_method: PrepareMethod
    ) -> None:
        _canonical_path = Path(self.path)
        _target_on_mnt = Path(target_mnt) / _canonical_path.relative_to(CANONICAL_ROOT)

        if prepare_method == "copy":
            shutil.copy(_rs, _target_on_mnt)
            self._set_perm(_target_on_mnt)
            self._set_xattr(_target_on_mnt)

        elif prepare_method == "hardlink":
            # NOTE: os.link will make dst a hardlink to src.
            os.link(_rs, _target_on_mnt)
            # NOTE: although we actually don't need to set_perm and set_xattr everytime
            #   to file paths point to the same inode, for simplicity here we just
            #   do it everytime.
            self._set_perm(_target_on_mnt)
            self._set_xattr(_target_on_mnt)

        elif prepare_method == "move":
            shutil.move(str(_rs), _target_on_mnt)
            self._set_perm(_target_on_mnt)
            self._set_xattr(_target_on_mnt)


class FileTableNonRegularFiles(TableSpec, FileTableBase):
    """DB table for non-regular file entries.

    This includes:
    1. directory.
    2. symlink.
    3. chardev file.

    NOTE that support for chardev file is only for overlayfs' whiteout file,
        so only device num as 0,0 will be allowed.
    """

    table_name: ClassVar[str] = "ft_non_regular"

    def prepare_target(self, *, target_mnt: StrOrPath) -> None:
        _canonical_path = Path(self.path)
        _target_on_mnt = Path(target_mnt) / _canonical_path.relative_to(CANONICAL_ROOT)

        inode_table = self.inode
        _mode = inode_table.mode
        if stat.S_ISDIR(_mode):
            _target_on_mnt.mkdir(exist_ok=True, parents=True)
            self._set_perm(_target_on_mnt)
            self._set_xattr(_target_on_mnt)
            return

        if stat.S_ISLNK(_mode):
            assert (_symlink_target_raw := self.contents), f"invalid entry {self}"
            _symlink_target = _symlink_target_raw.decode()
            _target_on_mnt.symlink_to(_symlink_target)
            self._set_perm(_target_on_mnt)
            self._set_xattr(_target_on_mnt)
            return

        if stat.S_ISCHR(_mode):
            _device_num = os.makedev(0, 0)
            os.mknod(_target_on_mnt, mode=_mode, device=_device_num)
            self._set_perm(_target_on_mnt)
            self._set_xattr(_target_on_mnt)
            return

        raise ValueError(f"invalid entry {self}")
