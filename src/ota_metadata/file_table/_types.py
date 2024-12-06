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

from typing import Dict, NamedTuple, Optional

from msgpack import Unpacker, packb
from pydantic import PlainSerializer, PlainValidator
from pydantic_core import core_schema
from typing_extensions import Annotated

XATTR_MAX_SIZE = 512 * 1024  # 512KiB
INODE_MAX_SIZE = 128  # bytes

#
# ------ inode table support ------ #
#


def _inode_validator(_in: bytes | InodeTable) -> InodeTable:
    if isinstance(_in, InodeTable):
        return _in

    _unpacker = Unpacker(max_buffer_size=INODE_MAX_SIZE)
    _unpacker.feed(_in)  # feed all the data into the internal buffer

    # get exactly one list from buffer.
    # NOTE that msgpack only has two container types when unpacking: list and dict.
    _obj = _unpacker.unpack()
    if not isinstance(_obj, list):
        raise TypeError(f"expect unpack to a list, get {type(_obj)=}")
    return InodeTable(*_obj)


def _inode_serializer(_in: InodeTable) -> bytes:
    if _res := packb(_in, buf_size=INODE_MAX_SIZE):
        return _res
    raise ValueError


class InodeTable(NamedTuple):
    mode: int
    uid: int
    gid: int
    size: Optional[int] = None
    inode: Optional[int] = None


InodeTableType = Annotated[
    InodeTable,
    PlainValidator(_inode_validator),
    PlainSerializer(_inode_serializer),
]

#
# ------ xattr support ------ #
#


def _xattr_validator(_in: bytes | Xattr) -> Xattr:
    if isinstance(_in, dict):
        return _in

    _unpacker = Unpacker(max_buffer_size=XATTR_MAX_SIZE)
    _unpacker.feed(_in)  # feed all the data into the internal buffer

    # get exactly one dict from buffer
    # NOTE that msgpack only has two container types when unpacking: list and dict.
    _obj = _unpacker.unpack()
    if not isinstance(_obj, dict):
        raise ValueError
    return Xattr(**_obj)


def _xattr_serializer(_in: Xattr) -> bytes:
    if _res := packb(_in, buf_size=XATTR_MAX_SIZE):
        return _res
    raise ValueError


class Xattr(Dict[str, str]):
    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        return core_schema.no_info_after_validator_function(cls, handler(dict))


XattrType = Annotated[
    Xattr,
    PlainValidator(_xattr_validator),
    PlainSerializer(_xattr_serializer),
]
