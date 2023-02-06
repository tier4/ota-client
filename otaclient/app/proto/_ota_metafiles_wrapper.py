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


def de_escape(s: str) -> str:
    return s.replace(r"'\''", r"'")


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


# backward compatibility

_dir_pa = re.compile(r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+),(?P<path>.*)")
_symlink_pa = re.compile(r"'(?P<link>.+)((?<!\')',')(?P<target>.+)'")
_persist_pa = re.compile(r"'(?P<path>)'")
# NOTE(20221013): support previous regular_inf cvs version
#                 that doesn't contain size, inode and compressed_alg fields.
_reginf_pa = re.compile(
    r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+)"
    r",(?P<nlink>\d+),(?P<hash>\w+),'(?P<path>.+)'"
    r"(,(?P<size>\d+)?(,(?P<inode>\d+)?(,(?P<compressed_alg>\w+)?)?)?)?"
)


def parse_dirs_from_txt(_input: str):
    """Compatibility to the plaintext dirs.txt."""
    _ma = _dir_pa.match(_input)
    assert _ma is not None, f"matching reg_inf failed for {_input}"

    mode = int(_ma.group("mode"), 8)
    uid = int(_ma.group("uid"))
    gid = int(_ma.group("gid"))
    path = de_escape(_ma.group("path")[1:-1])
    return DirectoryInf(mode=mode, uid=uid, gid=gid, path=path)


def parse_persistents_from_txt(_input: str):
    """Compatibility to the plaintext persists.txt."""
    _path = de_escape(_input.strip()[1:-1])
    return PersistentInf(path=_path)


def parse_symlinks_from_txt(_input: str):
    """Compatibility to the plaintext symlinks.txt."""
    _ma = _symlink_pa.match(_input)
    assert _ma is not None, f"matching reg_inf failed for {_input}"

    slink = _ma.group("link")
    srcpath = _ma.group("target")
    return SymbolicLinkInf(slink=slink, srcpath=srcpath)


def parse_regulars_from_txt(_input: str):
    """Compatibility to the plaintext regulars.txt."""
    res = RegularInf()
    _ma = _reginf_pa.match(_input)
    assert _ma is not None, f"matching reg_inf failed for {_input}"

    res.mode = int(_ma.group("mode"), 8)
    res.uid = int(_ma.group("uid"))
    res.gid = int(_ma.group("gid"))
    res.nlink = int(_ma.group("nlink"))
    res.sha256hash = bytes.fromhex(_ma.group("hash"))
    res.path = de_escape(_ma.group("path"))

    if _size := _ma.group("size"):
        res.size = int(_size)
        # ensure that size exists before parsing inode
        # and compressed_alg field.
        # it's OK to skip checking as un-existed fields
        # will be None anyway.
        res.inode = int(_inode) if (_inode := _ma.group("inode")) else 0
        res.compressed_alg = (
            _compress_alg if (_compress_alg := _ma.group("compressed_alg")) else ""
        )

    return res
