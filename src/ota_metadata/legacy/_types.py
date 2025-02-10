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
from pathlib import Path
from typing import Union

from ota_metadata.legacy import ota_metafiles_pb2 as ota_metafiles
from otaclient_common.proto_wrapper import MessageWrapper, calculate_slots

# helper mixin


class _UniqueByPath:
    path: str

    def __hash__(self) -> int:
        return hash(self.path)

    def __eq__(self, _other: object) -> bool:
        if isinstance(_other, Path):
            return self.path == str(_other)
        if isinstance(_other, str):
            return self.path == _other
        if isinstance(_other, self.__class__):
            return self.path == _other.path
        return False


# wrapper


class DirectoryInf(_UniqueByPath, MessageWrapper[ota_metafiles.DirectoryInf]):
    __slots__ = calculate_slots(ota_metafiles.DirectoryInf)
    gid: int
    mode: int
    path: str
    uid: int

    def mkdir_relative_to_mount_point(self, mount_point: Union[Path, str]):
        _target = os.path.join(mount_point, os.path.relpath(self.path, "/"))
        os.makedirs(_target, exist_ok=True)
        os.chmod(_target, self.mode)
        os.chown(_target, self.uid, self.gid)


class PersistentInf(MessageWrapper[ota_metafiles.PersistentInf]):
    __slots__ = calculate_slots(ota_metafiles.PersistentInf)
    path: str


class SymbolicLinkInf(MessageWrapper[ota_metafiles.SymbolicLinkInf]):
    __slots__ = calculate_slots(ota_metafiles.SymbolicLinkInf)
    gid: int
    mode: int
    slink: str
    srcpath: str
    uid: int

    def link_at_mount_point(self, mount_point: Union[Path, str]):
        """NOTE: symbolic link in /boot directory is not supported.
        We don't use it."""
        _newlink = os.path.join(mount_point, os.path.relpath(self.slink, "/"))
        os.symlink(self.srcpath, _newlink)
        os.chown(_newlink, self.uid, self.gid, follow_symlinks=False)


class RegularInf(_UniqueByPath, MessageWrapper[ota_metafiles.RegularInf]):
    __slots__ = calculate_slots(ota_metafiles.RegularInf)
    compressed_alg: str
    gid: int
    inode: int
    mode: int
    nlink: int
    path: str
    sha256hash: bytes
    size: int
    uid: int

    def relative_to(self, _target: Union[Path, str]) -> str:
        return os.path.relpath(self.path, _target)

    def get_hash(self) -> str:
        return self.sha256hash.hex()

    def relatively_join(self, mount_point: Union[Path, str]) -> str:
        """Return a path string relative to / or /boot and joined to <mount_point>."""
        if self.path.startswith("/boot"):
            return os.path.join(mount_point, os.path.relpath(self.path, "/boot"))
        else:
            return os.path.join(mount_point, os.path.relpath(self.path, "/"))

    def copy_relative_to_mount_point(
        self, dst_mount_point: Union[Path, str], /, *, src_mount_point: Union[Path, str]
    ):
        """Copy file to the path that relative to dst_mount_point, from src_mount_point."""
        _src = self.relatively_join(src_mount_point)
        _dst = self.relatively_join(dst_mount_point)
        shutil.copy2(_src, _dst, follow_symlinks=False)
        os.chown(_dst, self.uid, self.gid)
        os.chmod(_dst, self.mode)

    def copy_to_dst(
        self, dst: Union[Path, str], /, *, src_mount_point: Union[Path, str]
    ):
        """Copy file pointed by self to the dst."""
        _src = self.relatively_join(src_mount_point)
        shutil.copy2(_src, dst, follow_symlinks=False)
        os.chown(dst, self.uid, self.gid)
        os.chmod(dst, self.mode)

    def copy_from_src(
        self, src: Union[Path, str], *, dst_mount_point: Union[Path, str]
    ):
        """Copy file from src to dst pointed by regular_inf."""
        _dst = self.relatively_join(dst_mount_point)
        shutil.copy2(src, _dst, follow_symlinks=False)
        os.chown(_dst, self.uid, self.gid)
        os.chmod(_dst, self.mode)

    def move_from_src(
        self, src: Union[Path, str], *, dst_mount_point: Union[Path, str]
    ):
        """Copy file from src to dst pointed by regular_inf."""
        _dst = self.relatively_join(dst_mount_point)
        shutil.move(str(src), _dst)
        os.chown(_dst, self.uid, self.gid)
        os.chmod(_dst, self.mode)
