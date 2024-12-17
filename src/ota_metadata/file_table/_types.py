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

from typing import Generator, NamedTuple, Optional

from msgpack import Unpacker, packb
from pydantic import PlainSerializer, PlainValidator
from typing_extensions import Annotated

FILE_ENTRY_MAX_SIZE = 1024**2  # 1MiB


class FileEntryAttrs(NamedTuple):
    mode: int
    uid: int
    gid: int
    size: Optional[int] = None
    inode: Optional[int] = None
    xattrs: Optional[dict[str, str]] = None
    contents: Optional[bytes] = None

    def iter_xattrs(self) -> Generator[tuple[str, str]]:
        if self.xattrs:
            yield from self.xattrs.items()

    @classmethod
    def _validator(cls, _in: bytes | FileEntryAttrs) -> FileEntryAttrs:
        if isinstance(_in, FileEntryAttrs):
            return _in

        _unpacker = Unpacker(max_buffer_size=FILE_ENTRY_MAX_SIZE)
        _unpacker.feed(_in)  # feed all the data into the internal buffer

        # get exactly one list from buffer.
        # NOTE that msgpack only has two container types when unpacking: list and dict.
        _obj = _unpacker.unpack()
        if not isinstance(_obj, list):
            raise TypeError(f"expect unpack to a list, get {type(_obj)=}")
        return cls(*_obj)

    def _serializer(self: FileEntryAttrs) -> bytes:
        try:
            if _res := packb(self, buf_size=FILE_ENTRY_MAX_SIZE):
                return _res
            raise ValueError("nothing is packed")
        except Exception as e:
            raise ValueError(f"failed to pack {self}: {e!r}") from e


EntryAttrsType = Annotated[
    FileEntryAttrs,
    PlainValidator(FileEntryAttrs._validator),
    PlainSerializer(FileEntryAttrs._serializer),
]
