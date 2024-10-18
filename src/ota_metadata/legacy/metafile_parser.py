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
"""OTA image CSV metafiles parser implementation."""


from __future__ import annotations

import re

from ota_metadata._file_table.tables import (
    DirectoryTable,
    RegularFileTable,
    SymlinkTable,
)
from ota_metadata.legacy.orm import ResourceTable

BATCH_SIZE = 128
DIGEST_ALG = b"sha256"

# CSV format OTA image metadata files regex pattern


def de_escape(s: str) -> str:
    return s.replace(r"'\''", r"'")


dir_pa = re.compile(r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+),(?P<path>.*)")
symlink_pa = re.compile(
    r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+),'(?P<link>.+)((?<!\')',')(?P<target>.+)'"
)
# NOTE(20221013): support previous regular_inf cvs version
#                 that doesn't contain size, inode and compressed_alg fields.
reginf_pa = re.compile(
    r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+)"
    r",(?P<nlink>\d+),(?P<hash>\w+),'(?P<path>.+)'"
    r"(,(?P<size>\d+)?(,(?P<inode>\d+)?(,(?P<compressed_alg>\w+)?)?)?)?"
)


def parse_dir_line(_input: str) -> DirectoryTable:
    _ma = dir_pa.match(_input.strip())
    assert _ma is not None, f"matching dirs failed for {_input}"

    return DirectoryTable(
        mode=int(_ma.group("mode"), 8),
        uid=int(_ma.group("uid")),
        gid=int(_ma.group("gid")),
        path=de_escape(_ma.group("path")[1:-1]),
    )


def parse_symlink_line(_input: str) -> SymlinkTable:
    _ma = symlink_pa.match(_input.strip())
    assert _ma is not None, f"matching symlinks failed for {_input}"

    return SymlinkTable(
        path=de_escape(_ma.group("link")),
        uid=int(_ma.group("uid")),
        gid=int(_ma.group("gid")),
        target=de_escape(_ma.group("target")),
    )


def parse_regular_line(_input: str) -> tuple[RegularFileTable, ResourceTable]:
    _ma = reginf_pa.match(_input.strip())
    assert _ma is not None, f"matching reg_inf failed for {_input}"

    _new = RegularFileTable(
        mode=int(_ma.group("mode"), 8),
        uid=int(_ma.group("uid")),
        gid=int(_ma.group("gid")),
        nlink=int(_ma.group("nlink")),
        digest=b":".join([DIGEST_ALG, bytes.fromhex(_ma.group("hash"))]),
        path=de_escape(_ma.group("path")),
        size=int(_ma.group("size")),
    )

    if (_inode := _ma.group("inode")) and (_inode := int(_inode)) > 1:
        _new.inode = _inode

    if _compress_alg := _ma.group("compressed_alg"):
        _new_resource = ResourceTable(
            sha256digest=_ma.group("hash"),
            size=_new.size,
            compression_alg=_compress_alg,
        )
    else:
        _new_resource = ResourceTable(
            sha256digest=_ma.group("hash"),
            size=_new.size,
            path=_new.path,
        )

    return _new, _new_resource
