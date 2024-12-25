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
from abc import abstractmethod
from pathlib import Path
from typing import Any, ClassVar, Literal, Optional

from pydantic import BaseModel, SkipValidation
from simple_sqlite3_orm import ConstrainRepr, TableSpec, TypeAffinityRepr
from typing_extensions import Annotated

from ota_metadata.file_table._types import EntryAttrsType
from otaclient_common.logging import get_burst_suppressed_logger
from otaclient_common.typing import StrOrPath

burst_suppressed_logger = get_burst_suppressed_logger(f"{__name__}.file_op_failed")

CANONICAL_ROOT = "/"


class FileTableBase(BaseModel):
    schema_ver: ClassVar[Literal[1]] = 1

    path: Annotated[
        str,
        TypeAffinityRepr(str),
        ConstrainRepr("PRIMARY KEY"),
        SkipValidation,
    ]

    entry_attrs: Annotated[
        EntryAttrsType,
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

    See file_table._types for more details.
    """

    def set_xattr(self, _target: StrOrPath) -> None:
        """Set the xattr of self onto the <_target>.

        NOTE: this method always don't follow symlink.
        """
        if xattrs := self.entry_attrs.xattrs:
            for k, v in xattrs.items():
                os.setxattr(
                    path=_target,
                    attribute=k,
                    value=v.encode(),
                    follow_symlinks=False,
                )

    def set_perm(self, _target: StrOrPath) -> None:
        """Set the mode,uid,gid of self onto the <_target>."""
        entry_attrs = self.entry_attrs
        # NOTE(20241213): chown will reset the sticky bit of the file!!!
        #   Remember to always put chown before chmod !!!
        os.chown(_target, uid=entry_attrs.uid, gid=entry_attrs.gid)
        os.chmod(_target, mode=entry_attrs.mode)

    def fpath_on_target(self, target_mnt: StrOrPath) -> Path:
        """Return the fpath of self joined to <target_mnt>."""
        _canonical_path = Path(self.path)
        _target_on_mnt = Path(target_mnt) / _canonical_path.relative_to(CANONICAL_ROOT)
        return _target_on_mnt

    @abstractmethod
    def prepare_target(self, *args: Any, target_mnt: StrOrPath, **kwargs) -> None:
        raise NotImplementedError


class FileTableRegularFiles(TableSpec, FileTableBase):
    """DB table for regular file entries."""

    digest: Annotated[
        bytes,
        TypeAffinityRepr(bytes),
        SkipValidation,
    ]

    def prepare_target(
        self,
        _rs: StrOrPath,
        *,
        target_mnt: StrOrPath,
        prepare_method: Literal["move", "hardlink", "copy"],
    ) -> None:
        try:
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
        except Exception as e:
            burst_suppressed_logger.exception(
                f"failed on preparing {self!r}, {_rs=}: {e!r}"
            )


class FileTableNonRegularFiles(TableSpec, FileTableBase):
    """DB table for non-regular file entries.

    This includes:
    1. symlink.
    2. chardev file.

    NOTE that support for chardev file is only for overlayfs' whiteout file,
        so only device num as 0,0 will be allowed.
    """

    contents: Annotated[
        Optional[bytes],
        TypeAffinityRepr(bytes),
        SkipValidation,
    ] = None
    """The contents of the file. Currently only used by symlink."""

    def set_perm(self, _target: StrOrPath) -> None:
        """Set the mode,uid,gid of self onto the <_target>.

        NOTE: this method always don't follow symlink.
        """
        entry_attrs = self.entry_attrs

        # NOTE(20241213): chown will reset the sticky bit of the file!!!
        #   Remember to always put chown before chmod !!!
        os.chown(
            _target, uid=entry_attrs.uid, gid=entry_attrs.gid, follow_symlinks=False
        )
        # NOTE: changing mode of symlink is not needed and uneffective, and on some platform
        #   changing mode of symlink will even result in exception raised.
        if not stat.S_ISLNK(entry_attrs.mode):
            os.chmod(_target, mode=entry_attrs.mode)

    def prepare_target(self, *, target_mnt: StrOrPath) -> None:
        try:
            _target_on_mnt = self.fpath_on_target(target_mnt=target_mnt)

            entry_attrs = self.entry_attrs
            _mode = entry_attrs.mode
            if stat.S_ISLNK(_mode):
                assert (
                    _symlink_target_raw := self.contents
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
        except Exception as e:
            burst_suppressed_logger.exception(f"failed on preparing {self!r}: {e!r}")


class FileTableDirectories(TableSpec, FileTableBase):
    """DB table for directory entries."""

    def prepare_target(self, *, target_mnt: StrOrPath) -> None:
        try:
            _target_on_mnt = self.fpath_on_target(target_mnt=target_mnt)
            _target_on_mnt.mkdir(exist_ok=True, parents=True)
            self.set_perm(_target_on_mnt)
            self.set_xattr(_target_on_mnt)
        except Exception as e:
            burst_suppressed_logger.exception(f"failed on preparing {self!r}: {e!r}")
