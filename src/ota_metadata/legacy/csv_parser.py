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
"""Parse the CSV based OTA image metafiles and import into database."""


from __future__ import annotations

import re
import stat

from ota_metadata.file_table import (
    FileEntryAttrs,
    FileTableNonRegularFiles,
    FileTableRegularFiles,
    FTDirORM,
    FTNonRegularORM,
    FTRegularORM,
)

from .rs_table import RSTORM, ResourceTable

BATCH_SIZE = 2048


def de_escape(s: str) -> str:
    return s.replace(r"'\''", r"'")


# format definition in regular pattern

_dir_pa = re.compile(r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+),(?P<path>.*)")
_symlink_pa = re.compile(
    r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+),'(?P<link>.+)((?<!\')',')(?P<target>.+)'"
)
# NOTE(20221013): support previous regular_inf cvs version
#                 that doesn't contain size, inode and compressed_alg fields.
_reginf_pa = re.compile(
    r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+)"
    r",(?P<nlink>\d+),(?P<hash>\w+),'(?P<path>.+)'"
    r"(,(?P<size>\d+)?(,(?P<inode>\d+)?(,(?P<compressed_alg>\w+)?)?)?)?"
)


def parse_dirs_from_csv_file(_fpath: str, _orm: FTDirORM) -> int:
    """Compatibility to the plaintext dirs.txt."""
    _batch, _batch_cnt = [], 0
    with open(_fpath, "r") as f:
        _idx = 0
        for _idx, line in enumerate(f, start=1):
            _ma = _dir_pa.match(line.strip())
            assert _ma is not None, f"matching dirs failed for {line}"

            mode = int(_ma.group("mode"), 8) | stat.S_IFDIR
            uid = int(_ma.group("uid"))
            gid = int(_ma.group("gid"))
            path = de_escape(_ma.group("path")[1:-1])

            _new = FileTableNonRegularFiles(
                path=path,
                entry_attrs=FileEntryAttrs(
                    uid=uid,
                    gid=gid,
                    mode=mode,
                ).pack(),
            )

            _batch.append(_new)
            if (_this_batch := _idx // BATCH_SIZE) > _batch_cnt:
                _batch_cnt = _this_batch
                _orm.orm_insert_entries(_batch)
                _batch = []
        _orm.orm_insert_entries(_batch)
        return _idx


def parse_symlinks_from_csv_file(_fpath: str, _orm: FTNonRegularORM) -> int:
    """Compatibility to the plaintext symlinks.txt."""
    _batch, _batch_cnt = [], 0
    with open(_fpath, "r") as f:
        _idx = 0
        for _idx, line in enumerate(f, start=1):
            _ma = _symlink_pa.match(line.strip())
            assert _ma is not None, f"matching symlinks failed for {line}"

            mode = int(_ma.group("mode"), 8) | stat.S_IFLNK
            uid = int(_ma.group("uid"))
            gid = int(_ma.group("gid"))
            slink = de_escape(_ma.group("link"))
            srcpath = de_escape(_ma.group("target"))

            _new = FileTableNonRegularFiles(
                path=slink,
                entry_attrs=FileEntryAttrs(
                    mode=mode,
                    uid=uid,
                    gid=gid,
                    contents=srcpath.encode("utf-8"),
                ).pack(),
            )

            _batch.append(_new)
            if (_this_batch := _idx // BATCH_SIZE) > _batch_cnt:
                _batch_cnt = _this_batch
                _orm.orm_insert_entries(_batch)
                _batch = []
        _orm.orm_insert_entries(_batch)
        return _idx


def parse_regulars_from_csv_file(
    _fpath: str, _orm: FTRegularORM, _orm_rs: RSTORM
) -> int:
    """Compatibility to the plaintext regulars.txt."""
    _batch, _batch_rs, _batch_cnt = [], [], 0
    with open(_fpath, "r") as f:
        _idx = 0
        for _idx, line in enumerate(f, start=1):
            _ma = _reginf_pa.match(line.strip())
            assert _ma is not None, f"matching reg_inf failed for {line}"

            mode = int(_ma.group("mode"), 8) | stat.S_IFREG
            uid = int(_ma.group("uid"))
            gid = int(_ma.group("gid"))
            sha256hash = bytes.fromhex(_ma.group("hash"))
            path = de_escape(_ma.group("path"))

            size, inode, compressed_alg = None, None, None
            if _size := _ma.group("size"):
                size = int(_size)
                # ensure that size exists before parsing inode
                # and compressed_alg field.
                inode = int(_inode) if (_inode := _ma.group("inode")) else None
                compressed_alg = (
                    _compress_alg
                    if (_compress_alg := _ma.group("compressed_alg"))
                    else ""
                )

            _new = FileTableRegularFiles(
                path=path,
                entry_attrs=FileEntryAttrs(
                    mode=mode,
                    uid=uid,
                    gid=gid,
                    size=size,
                    inode=inode,
                ).pack(),
                digest=sha256hash,
            )

            _new_rs = ResourceTable(
                path=path if not compressed_alg else None,
                digest=sha256hash,
                compression_alg=compressed_alg,
                original_size=size or 0,
            )

            _batch.append(_new)
            _batch_rs.append(_new_rs)

            if (_this_batch := _idx // BATCH_SIZE) > _batch_cnt:
                _batch_cnt = _this_batch
                _orm.orm_insert_entries(_batch)
                # NOTE: ignore entries with same digest
                _orm_rs.orm_insert_entries(_batch_rs, or_option="ignore")

                _batch, _batch_rs = [], []
        _orm.orm_insert_entries(_batch)
        _orm_rs.orm_insert_entries(_batch_rs, or_option="ignore")

        return _idx
