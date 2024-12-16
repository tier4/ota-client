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
from typing import ClassVar, Literal

from pydantic import BaseModel
from simple_sqlite3_orm import ConstrainRepr, TableSpec, TypeAffinityRepr
from typing_extensions import Annotated

from ota_metadata.file_table._types import FileEntryAttrs
from otaclient_common.typing import StrOrPath

CANONICAL_ROOT = "/"

PrepareMethod = Literal["move", "hardlink", "copy"]


class FileTableBase(BaseModel):
    schema_ver: ClassVar[Literal[1]] = 1

    path: Annotated[
        str,
        TypeAffinityRepr(str),
        ConstrainRepr("PRIMARY KEY"),
    ]

    entry_attrs: Annotated[
        bytes,
        TypeAffinityRepr(bytes),
        ConstrainRepr("NOT NULL"),
    ]
    """msgpacked basic attrs for this file entry.

    Ref: https://www.kernel.org/doc/html/latest/filesystems/ext4/inodes.html

    including the following fields from inode table:
    1. mode bits
    2. uid
    3. gid
    4. inode(when the file is hardlinked)
    5. xattrs
    6. contents

    See file_table._types for more details.
    """

    _parsed_entry_attrs: FileEntryAttrs | None = None

    @property
    def parsed_entry_attrs(self) -> FileEntryAttrs:
        """
        NOTE: the parsed entry_attrs will be cached once read.
        """
        if not self._parsed_entry_attrs:
            self._parsed_entry_attrs = FileEntryAttrs.unpack(self.entry_attrs)
        return self._parsed_entry_attrs

    def set_xattr(self, _target: StrOrPath) -> None:
        """Set the xattr of self onto the <_target>.

        NOTE: this method always don't follow symlink.
        """
        for k, v in self.parsed_entry_attrs.iter_xattrs():
            os.setxattr(
                path=_target,
                attribute=k,
                value=v.encode(),
                follow_symlinks=False,
            )

    def set_perm(self, _target: StrOrPath) -> None:
        """Set the mode,uid,gid of self onto the <_target>.

        NOTE: this method always don't follow symlink.
        """
        entry_attrs = self.parsed_entry_attrs
        # NOTE(20241213): chown will reset the sticky bit of the file!!!
        #   Remember to always put chown before chmod !!!
        os.chown(
            _target, uid=entry_attrs.uid, gid=entry_attrs.gid, follow_symlinks=False
        )
        os.chmod(_target, mode=entry_attrs.mode, follow_symlinks=False)

    def fpath_on_target(self, target_mnt: StrOrPath) -> Path:
        """Return the fpath of self joined to <target_mnt>."""
        _canonical_path = Path(self.path)
        _target_on_mnt = Path(target_mnt) / _canonical_path.relative_to(CANONICAL_ROOT)
        return _target_on_mnt


class FileTableRegularFiles(TableSpec, FileTableBase):
    """DB table for regular file entries."""

    digest: Annotated[
        bytes,
        TypeAffinityRepr(bytes),
    ]

    def prepare_target(
        self,
        _rs: StrOrPath,
        *,
        target_mnt: StrOrPath,
        prepare_method: PrepareMethod,
    ) -> None:
        _target_on_mnt = self.fpath_on_target(target_mnt=target_mnt)

        if prepare_method == "copy":
            shutil.copy(_rs, _target_on_mnt)
            self.set_perm(_target_on_mnt)
            self.set_xattr(_target_on_mnt)
            return

        if prepare_method == "hardlink":
            # NOTE: os.link will make dst a hardlink to src.
            os.link(_rs, _target_on_mnt)
            # NOTE: although we actually don't need to set_perm and set_xattr everytime
            #   to file paths point to the same inode, for simplicity here we just
            #   do it everytime.
            self.set_perm(_target_on_mnt)
            self.set_xattr(_target_on_mnt)
            return

        if prepare_method == "move":
            shutil.move(str(_rs), _target_on_mnt)
            self.set_perm(_target_on_mnt)
            self.set_xattr(_target_on_mnt)


class FileTableNonRegularFiles(TableSpec, FileTableBase):
    """DB table for non-regular file entries.

    This includes:
    1. symlink.
    2. chardev file.

    NOTE that support for chardev file is only for overlayfs' whiteout file,
        so only device num as 0,0 will be allowed.
    """

    def set_perm(self, _target: StrOrPath) -> None:
        """Set the mode,uid,gid of self onto the <_target>.

        NOTE: this method always don't follow symlink.
        """
        entry_attrs = self.parsed_entry_attrs

        # NOTE(20241213): chown will reset the sticky bit of the file!!!
        #   Remember to always put chown before chmod !!!
        os.chown(
            _target, uid=entry_attrs.uid, gid=entry_attrs.gid, follow_symlinks=False
        )
        # NOTE: changing mode of symlink is not needed and uneffective, and on some platform
        #   changing mode of symlink will even result in exception raised.
        if not stat.S_ISLNK(entry_attrs.mode):
            os.chmod(_target, mode=entry_attrs.mode, follow_symlinks=False)

    def prepare_target(self, *, target_mnt: StrOrPath) -> None:
        _target_on_mnt = self.fpath_on_target(target_mnt=target_mnt)

        entry_attrs = self.parsed_entry_attrs
        _mode = entry_attrs.mode
        if stat.S_ISLNK(_mode):
            assert (
                _symlink_target_raw := entry_attrs.contents
            ), f"invalid entry {self}, entry is a symlink but no link target is defined"

            _symlink_target = _symlink_target_raw.decode()
            _target_on_mnt.symlink_to(_symlink_target)
            self.set_perm(_target_on_mnt)
            self.set_xattr(_target_on_mnt)
            return

        if stat.S_ISCHR(_mode):
            _device_num = os.makedev(0, 0)
            os.mknod(_target_on_mnt, mode=_mode, device=_device_num)
            self.set_perm(_target_on_mnt)
            self.set_xattr(_target_on_mnt)
            return

        raise ValueError(f"invalid entry {self}")


class FileTableDirectories(TableSpec, FileTableBase):
    """DB table for directory entries."""

    def prepare_target(self, *, target_mnt: StrOrPath) -> None:
        _target_on_mnt = self.fpath_on_target(target_mnt=target_mnt)
        _target_on_mnt.mkdir(exist_ok=True, parents=True)
        self.set_perm(_target_on_mnt)
        self.set_xattr(_target_on_mnt)
