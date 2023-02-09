from __future__ import annotations
import os
import re
import shutil
import typing
import ota_metafiles_pb2 as ota_metafiles
from pathlib import Path
from typing import Type, Union

from ._common import WrapperBase, MessageWrapper

# defined types in original compile proto module

_DirectoryInf = typing.cast(Type[ota_metafiles.DirectoryInf], WrapperBase)
_PersistentInf = typing.cast(Type[ota_metafiles.PersistentInf], WrapperBase)
_RegularInf = typing.cast(Type[ota_metafiles.RegularInf], WrapperBase)
_SymbolicLinkInf = typing.cast(Type[ota_metafiles.SymbolicLinkInf], WrapperBase)


# helper


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


# wrapped


class DirectoryInf(_UniqueByPath, _DirectoryInf, MessageWrapper):
    proto_class = ota_metafiles.DirectoryInf
    data: ota_metafiles.DirectoryInf

    def mkdir_relative_to_mount_point(self, mount_point: Union[Path, str]):
        _target = os.path.join(mount_point, os.path.relpath(self.path, "/"))
        os.makedirs(_target, exist_ok=True)
        os.chmod(_target, self.mode)
        os.chown(_target, self.uid, self.gid)


class PersistentInf(_PersistentInf, MessageWrapper):
    proto_class = ota_metafiles.PersistentInf
    data: ota_metafiles.PersistentInf


class SymbolicLinkInf(_SymbolicLinkInf, MessageWrapper):
    proto_class = ota_metafiles.SymbolicLinkInf
    data: ota_metafiles.SymbolicLinkInf

    def link_at_mount_point(self, mount_point: Union[Path, str]):
        """NOTE: symbolic link in /boot directory is not supported.
        We don't use it."""
        _newlink = os.path.join(mount_point, os.path.relpath(self.slink, "/"))
        os.symlink(self.srcpath, self.slink)
        os.chown(_newlink, self.uid, self.gid, follow_symlinks=False)


class RegularInf(_UniqueByPath, _RegularInf, MessageWrapper):
    proto_class = ota_metafiles.RegularInf
    data: ota_metafiles.RegularInf

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
