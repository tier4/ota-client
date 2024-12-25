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
from pathlib import Path

from ota_metadata.file_table import (
    FileEntryAttrs,
    FileTableDirectories,
    FileTableDirORM,
    FileTableNonRegularFiles,
    FileTableNonRegularORM,
    FileTableRegularFiles,
    FileTableRegularORM,
)
from otaclient_common.typing import StrOrPath

from .rs_table import ResourceTable, ResourceTableORM

ENTRIES_PROCESS_BATCH_SIZE = 2048


def de_escape(s: str) -> str:
    """de-escape the previously escaped '\' character."""
    return s.replace(r"'\''", r"'")


#
# ------ dirs.txt CSV parser implementation ------ #
#

DIR_CSV_PATTERN = re.compile(r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+),(?P<path>.*)")


def parse_dirs_csv_line(line: str) -> FileTableDirectories:
    """Directory entry's CSV pattern.

    Examples:
    0755,1000,1000,'.'
    0755,1000,1000,'my-dir'
    0755,1000,1000,'my-dir/my-sub-dir'
    0755,1000,1000,'single'\''quote'
    """
    _ma = DIR_CSV_PATTERN.match(line.strip())
    assert _ma is not None, f"matching dirs failed for {line}"

    # NOTE: the ota-metadata generator strips away the file type bits,
    #       we need to put it back into the mode.
    #       os.chmod will just ignore the file type bits.
    mode = int(_ma.group("mode"), 8) | stat.S_IFDIR
    uid = int(_ma.group("uid"))
    gid = int(_ma.group("gid"))
    path = de_escape(_ma.group("path")[1:-1])

    return FileTableDirectories(
        path=path,
        entry_attrs=FileEntryAttrs(
            uid=uid,
            gid=gid,
            mode=mode,
        ),
    )


def parse_dirs_from_csv_file(
    _fpath: StrOrPath, _orm: FileTableDirORM, *, cleanup: bool = True
) -> int:
    """Compatibility to the plaintext CSV dirs.txt."""
    _batch, _batch_cnt = [], 0
    try:
        with open(_fpath, "r") as f:
            _idx = 0
            for _idx, line in enumerate(f, start=1):
                _new = parse_dirs_csv_line(line)
                _batch.append(_new)
                if (_this_batch := _idx // ENTRIES_PROCESS_BATCH_SIZE) > _batch_cnt:
                    _batch_cnt = _this_batch
                    _orm.orm_insert_entries(_batch)
                    _batch = []
            _orm.orm_insert_entries(_batch)
            return _idx
    finally:
        if cleanup:
            Path(_fpath).unlink(missing_ok=True)


#
# ------ symlinks.txt CSV parser implementation ------ #
#

SYMLINK_CSV_PATTERN = re.compile(
    r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+),'(?P<link>.+)((?<!\')',')(?P<target>.+)'"
)


def parse_symlinks_csv_line(line: str) -> FileTableNonRegularFiles:
    """Symlink entry's CSV pattern.

    Examples:
    0777,1000,1000,'actual','link'
    0777,1000,1000,'foo','bar'
    0777,1000,1000,'bar','single'\''quote'
    """
    _ma = SYMLINK_CSV_PATTERN.match(line.strip())
    assert _ma is not None, f"matching symlinks failed for {line}"

    # NOTE: the ota-metadata generator strips away the file type bits.
    mode = int(_ma.group("mode"), 8) | stat.S_IFLNK
    uid = int(_ma.group("uid"))
    gid = int(_ma.group("gid"))
    slink = de_escape(_ma.group("link"))
    srcpath = de_escape(_ma.group("target"))

    return FileTableNonRegularFiles(
        path=slink,
        entry_attrs=FileEntryAttrs(
            mode=mode,
            uid=uid,
            gid=gid,
        ),
        contents=srcpath.encode("utf-8"),
    )


def parse_symlinks_from_csv_file(
    _fpath: StrOrPath, _orm: FileTableNonRegularORM, *, cleanup: bool = True
) -> int:
    """Compatibility to the plaintext symlinks.txt."""
    _batch, _batch_cnt = [], 0
    try:
        with open(_fpath, "r") as f:
            _idx = 0
            for _idx, line in enumerate(f, start=1):
                _new = parse_symlinks_csv_line(line)
                _batch.append(_new)
                if (_this_batch := _idx // ENTRIES_PROCESS_BATCH_SIZE) > _batch_cnt:
                    _batch_cnt = _this_batch
                    _orm.orm_insert_entries(_batch)
                    _batch = []
            _orm.orm_insert_entries(_batch)
            return _idx
    finally:
        if cleanup:
            Path(_fpath).unlink(missing_ok=True)


#
# ------ regulars.txt CSV parser implementation ------ #
#

# NOTE(20221013): support previous regular_inf cvs version
#                 that doesn't contain size, inode and compressed_alg fields.
REGINF_CSV_PATTERN = re.compile(
    r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+)"
    r",(?P<nlink>\d+),(?P<hash>\w+),'(?P<path>.+)'"
    r"(,(?P<size>\d+)?(,(?P<inode>\d+)?(,(?P<compressed_alg>\w+)?)?)?)?"
)


def parse_regulars_csv_line(line: str) -> tuple[FileTableRegularFiles, ResourceTable]:
    """Regular file entry's CSV pattern.

    Note that <compressed_alg> only has `zst` now.

    Examples:
    0644,1000,1000,1,e67fba5d420c04fbed75ecda357bdde3c9c90d65f4f290f14a19f28aef7019df,'README.md',1234,
    0644,1000,1000,1,0dd0eed717ffeaf2a95fd774108263053c866c40a82e34e58ca796eea42b7727,'sub-dir/README.md',4567,
    0644,1000,1000,2,d9cd8155764c3543f10fad8a480d743137466f8d55213c8eaefcd12f06d43a80,'single'\''quote',89012,11123
    0644,1000,1000,2,d9cd8155764c3543f10fad8a480d743137466f8d55213c8eaefcd12f06d43a80,'single'\''quotee',89012,11123,zst
    """
    _ma = REGINF_CSV_PATTERN.match(line.strip())
    assert _ma is not None, f"matching reg_inf failed for {line}"

    # NOTE: the ota-metadata generator strips away the file type bits.
    mode = int(_ma.group("mode"), 8) | stat.S_IFREG
    uid = int(_ma.group("uid"))
    gid = int(_ma.group("gid"))
    sha256hash = bytes.fromhex(_ma.group("hash"))
    path = de_escape(_ma.group("path"))

    # NOTE: <size> is added in legacy OTA image revision1, <inode> is added in revision2,
    #       <compressed_alg> is added in revision3.
    size, inode, compressed_alg = None, None, None
    if _size := _ma.group("size"):
        size = int(_size)
        # ensure that size exists before parsing inode
        # and compressed_alg field.
        inode = int(_inode) if (_inode := _ma.group("inode")) else None
        compressed_alg = (
            _compress_alg if (_compress_alg := _ma.group("compressed_alg")) else ""
        )

    _new = FileTableRegularFiles(
        path=path,
        entry_attrs=FileEntryAttrs(
            mode=mode,
            uid=uid,
            gid=gid,
            size=size,
            inode=inode,
        ),
        digest=sha256hash,
    )

    # NOTE: in rev4 of OTA image metadata we add compression support, and compressed version of resource
    #       can be retrieved by sha256digest in the OTA image's blob storage.
    #       However, pre-revision3, all the files are just stored with its full path under
    #       the `data` folder at OTA image.
    #
    #       For example, for file /a/b/c/d/e, without compression, it will be at:
    #           <OTA_image_root>/data/a/b/c/d/e.
    #
    #       For compressed file, for example, for file /c/d/e/f/big.img with sha256digest 0xead8125c54f953684b40e5f2cf430ccae4946b85b850c4a65f1b468ae4b7e4b1,
    #           it's compressed version of resource will be available at:
    #           <OTA_image_root>/data.zst/ead8125c54f953684b40e5f2cf430ccae4946b85b850c4a65f1b468ae4b7e4b1.zst
    _new_rs = ResourceTable(
        path=path if not compressed_alg else None,
        digest=sha256hash,
        compression_alg=compressed_alg,
        original_size=size or 0,
    )

    return _new, _new_rs


def parse_regulars_from_csv_file(
    _fpath: StrOrPath,
    _orm: FileTableRegularORM,
    _orm_rs: ResourceTableORM,
    *,
    cleanup: bool = True,
) -> int:
    """Compatibility to the plaintext regulars.txt."""
    _batch, _batch_rs, _batch_cnt = [], [], 0
    try:
        with open(_fpath, "r") as f:
            _idx = 0
            for _idx, line in enumerate(f, start=1):
                _new, _new_rs = parse_regulars_csv_line(line)
                _batch.append(_new)
                _batch_rs.append(_new_rs)

                if (_this_batch := _idx // ENTRIES_PROCESS_BATCH_SIZE) > _batch_cnt:
                    _batch_cnt = _this_batch
                    _orm.orm_insert_entries(_batch)
                    # NOTE: ignore entries with same digest
                    _orm_rs.orm_insert_entries(_batch_rs, or_option="ignore")

                    _batch, _batch_rs = [], []
            _orm.orm_insert_entries(_batch)
            _orm_rs.orm_insert_entries(_batch_rs, or_option="ignore")

            return _idx
    finally:
        if cleanup:
            Path(_fpath).unlink(missing_ok=True)


#
# ------ persists.txt CSV parser implementation ------ #
#


def parse_persists_csv_line(line: str) -> str:
    return de_escape(line.strip()[1:-1])
