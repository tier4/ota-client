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

import logging
import re
import stat
from typing import NamedTuple

from ota_image_libs.v1.file_table.db import (
    FileTableDirORM,
    FileTableInodeORM,
    FileTableNonRegularORM,
    FileTableRegularORM,
    FileTableResourceORM,
)
from ota_image_libs.v1.file_table.schema import (
    FileTableDirectoryTypedDict,
    FiletableInodeTypedDict,
    FileTableNonRegularTypedDict,
    FileTableRegularTypedDict,
    FileTableResource,
)

from otaclient_common._typing import StrOrPath

from .rs_table import ResourceTable, ResourceTableORM

logger = logging.getLogger(__name__)

ENTRIES_PROCESS_BATCH_SIZE = 8192


def de_escape(s: str) -> str:
    """de-escape the previously escaped '\' character."""
    return s.replace(r"'\''", r"'")


class DigestSize(NamedTuple):
    digest: bytes
    size: int


#
# ------ dirs.txt CSV parser implementation ------ #
#

DIR_CSV_PATTERN = re.compile(r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+),(?P<path>.*)")


def parse_dirs_csv_line(
    line: str, inode: int
) -> tuple[FileTableDirectoryTypedDict, FiletableInodeTypedDict]:
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

    return FileTableDirectoryTypedDict(
        path=path, inode_id=inode
    ), FiletableInodeTypedDict(mode=mode, uid=uid, gid=gid, inode_id=inode)


def parse_dirs_from_csv_file(
    _fpath: StrOrPath,
    _orm: FileTableDirORM,
    _inode_orm: FileTableInodeORM,
    *,
    inode_start: int,
) -> tuple[int, int]:
    """Compatibility to the plaintext CSV dirs.txt."""
    _batch: list[FileTableDirectoryTypedDict] = []
    _inode_batch: list[FiletableInodeTypedDict] = []
    _batch_cnt = 0

    with open(_fpath, "r") as f:
        _idx = 0
        for _idx, line in enumerate(f, start=1):
            _new, _new_inode = parse_dirs_csv_line(line, inode_start)
            inode_start += 1
            _batch.append(_new)
            _inode_batch.append(_new_inode)

            if (_this_batch := _idx // ENTRIES_PROCESS_BATCH_SIZE) > _batch_cnt:
                _batch_cnt = _this_batch
                _orm.orm_insert_mappings(_batch)
                _inode_orm.orm_insert_mappings(_inode_batch)

                _batch, _inode_batch = [], []

        _orm.orm_insert_mappings(_batch)
        _inode_orm.orm_insert_mappings(_inode_batch)
        return _idx, inode_start


#
# ------ symlinks.txt CSV parser implementation ------ #
#

SYMLINK_CSV_PATTERN = re.compile(
    r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+),'(?P<link>.+)((?<!\')',')(?P<target>.+)'"
)


def parse_symlinks_csv_line(
    line: str, inode: int
) -> tuple[FileTableNonRegularTypedDict, FiletableInodeTypedDict]:
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

    return FileTableNonRegularTypedDict(
        path=slink,
        meta=srcpath.encode(),
        inode_id=inode,
    ), FiletableInodeTypedDict(mode=mode, uid=uid, gid=gid, inode_id=inode)


def parse_symlinks_from_csv_file(
    _fpath: StrOrPath,
    _orm: FileTableNonRegularORM,
    _inode_orm: FileTableInodeORM,
    *,
    inode_start: int,
) -> tuple[int, int]:
    """Compatibility to the plaintext symlinks.txt."""
    _batch: list[FileTableNonRegularTypedDict] = []
    _inode_batch: list[FiletableInodeTypedDict] = []
    _batch_cnt = 0

    with open(_fpath, "r") as f:
        _idx = 0
        for _idx, line in enumerate(f, start=1):
            _new, _new_inode = parse_symlinks_csv_line(line, inode_start)
            inode_start += 1
            _batch.append(_new)
            _inode_batch.append(_new_inode)

            if (_this_batch := _idx // ENTRIES_PROCESS_BATCH_SIZE) > _batch_cnt:
                _batch_cnt = _this_batch
                _orm.orm_insert_mappings(_batch)
                _inode_orm.orm_insert_mappings(_inode_batch)

                _batch, _inode_batch = [], []

        _orm.orm_insert_mappings(_batch)
        _inode_orm.orm_insert_mappings(_inode_batch)
        return _idx, inode_start


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


def parse_regular_csv_line(line: str) -> re.Match:
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
    return _ma


def parser_create_file_table_rs_entry(_ma: re.Match) -> DigestSize:
    sha256hash = bytes.fromhex(_ma.group("hash"))
    size = 0
    if _size := _ma.group("size"):
        size = int(_size)
    return DigestSize(sha256hash, size)


def parse_create_file_table_row(
    _ma: re.Match,
) -> tuple[FileTableRegularTypedDict, FiletableInodeTypedDict, ResourceTable]:
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

    _new_inode = FiletableInodeTypedDict(uid=uid, gid=gid, mode=mode)
    # NOTE(20250512): for hardlinked entry, we use minus inode
    #                 as inode_id to indentify it.
    if inode:
        _new_inode["inode_id"] = -inode

    _new = FileTableRegularTypedDict(path=path)

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
    return _new, _new_inode, _new_rs


def parse_regulars_from_csv_file(
    _fpath: StrOrPath,
    _orm: FileTableRegularORM,
    _orm_ft_resource: FileTableResourceORM,
    _orm_inode: FileTableInodeORM,
    _orm_rs: ResourceTableORM,
    *,
    inode_start: int,
) -> tuple[int, int]:
    """Compatibility to the plaintext regulars.txt."""
    digest_resources: dict[DigestSize, int] = {}
    hardlinked_inode: dict[int, FiletableInodeTypedDict] = {}

    _batch_cnt = 0
    _batch: list[FileTableRegularTypedDict] = []
    _batch_rs: list[ResourceTable] = []
    _batch_inode: list[FiletableInodeTypedDict] = []

    with open(_fpath, "r") as f:
        regular_file_entry_count = 0
        for regular_file_entry_count, line in enumerate(f, start=1):
            _line_match = parse_regular_csv_line(line)
            _digest_size = parser_create_file_table_rs_entry(_line_match)

            _resource_id = digest_resources.setdefault(
                _digest_size, len(digest_resources)
            )

            _new, _new_inode, _new_rs = parse_create_file_table_row(_line_match)

            # inode_id is generated, means it is a hardlinked inode
            if (_inode_id := _new_inode.get("inode_id")) is not None:
                _inode_entry = hardlinked_inode.setdefault(_inode_id, _new_inode)
                _current_link_cnt = _inode_entry.get("links_count")
                if _current_link_cnt is None:
                    _current_link_cnt = 1
                else:
                    _current_link_cnt += 1
                _inode_entry["links_count"] = _current_link_cnt
            # normal inode
            else:
                _inode_id = inode_start
                _new_inode["inode_id"] = _inode_id
                _batch_inode.append(_new_inode)
                inode_start += 1

            # assign calculated inode_id and resource_id to the ft entry
            _new["inode_id"] = _inode_id
            _new["resource_id"] = _resource_id

            _batch.append(_new)
            _batch_rs.append(_new_rs)

            if (
                _this_batch := regular_file_entry_count // ENTRIES_PROCESS_BATCH_SIZE
            ) > _batch_cnt:
                _batch_cnt = _this_batch
                _orm.orm_insert_mappings(_batch)
                _orm_inode.orm_insert_mappings(_batch_inode)
                # NOTE: ignore entries with same digest
                _orm_rs.orm_insert_entries(_batch_rs, or_option="ignore")

                _batch, _batch_rs, _batch_inode = [], [], []

        # insert the leftover entries
        _orm.orm_insert_mappings(_batch)
        _orm_inode.orm_insert_mappings(_batch_inode)
        _orm_rs.orm_insert_entries(_batch_rs, or_option="ignore")

        # insert hardlinked inode tables
        _orm_inode.orm_insert_mappings(hardlinked_inode.values())

    # prepare the ft_resource
    _orm_ft_resource.orm_insert_entries(
        (
            FileTableResource(
                resource_id=_rs_id, digest=_digest_size.digest, size=_digest_size.size
            )
            for _digest_size, _rs_id in digest_resources.items()
        )
    )
    return regular_file_entry_count, inode_start


#
# ------ persists.txt CSV parser implementation ------ #
#


def parse_persists_csv_line(line: str) -> str:
    return de_escape(line.strip()[1:-1])
