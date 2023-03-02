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
"""uint32 len delimited streamer.

The length-delimited stream layout is as follow:
----------------------------------------------------
| len(msg_1) |   msg_1   | len(msg_2) |   msg_2   | ...
-----------------------------------------------------
Which <len> will be 4 bytes unsigned int in big-endian layout. 
"""


from ._common import MessageType, MessageWrapperType
from typing import Iterable, Optional, Type, Generic, BinaryIO

UINT32_LEN = 4  # bytes


class Uint32LenDelimitedReader:
    def __init__(self, _read_stream: BinaryIO, /) -> None:
        self._stream = _read_stream

    def read1_data(self) -> Optional[bytes]:
        """Read one message from the stream."""
        if (
            data_len := int.from_bytes(
                self._stream.read(UINT32_LEN),
                byteorder="big",
                signed=False,
            )
        ) == 0:  # reach EOF
            return

        _bin = self._stream.read(data_len)
        if not len(_bin) == data_len:
            return  # partial read detected, the stream is incompleted
        return _bin


class Uint32LenDelimitedMsgReader(
    Uint32LenDelimitedReader, Generic[MessageWrapperType, MessageType]
):
    def __init__(
        self, _read_stream: BinaryIO, /, wrapper_type: Type[MessageWrapperType]
    ) -> None:
        self._wrapper_type = wrapper_type
        self._stream = _read_stream

    def iter_msg(self) -> Iterable[MessageWrapperType]:
        """Read, parse and yield message until EOF."""
        while _bin := self.read1_data():
            try:
                yield self._wrapper_type.converted_from_deserialized(_bin)
            except Exception as e:
                raise ValueError(f"failed to read message: {e}") from e


class Uint32LenDelimitedWriter:
    def __init__(self, _write_stream: BinaryIO, /) -> None:
        self._stream = _write_stream

    def write1_data(self, _data: bytes, /) -> int:
        try:
            _data_len_in_bytes = len(_data).to_bytes(
                length=UINT32_LEN,
                byteorder="big",
                signed=False,
            )
        except OverflowError:  # message is too long
            return 0

        _bytes_written = self._stream.write(_data_len_in_bytes)
        _bytes_written += self._stream.write(_data)
        return _bytes_written


class Uint32LenDelimitedMsgWriter(
    Uint32LenDelimitedWriter, Generic[MessageWrapperType, MessageType]
):
    def __init__(
        self, _write_stream: BinaryIO, /, wrapper_type: Type[MessageWrapperType]
    ) -> None:
        self._stream = _write_stream
        self._wrapper_type = wrapper_type

    def write1_msg(self, _wrapper_inst: MessageWrapperType) -> int:
        if not isinstance(_wrapper_inst, self._wrapper_type):
            raise ValueError(f"expect input wrapper to be {self._wrapper_type} type")
        return self.write1_data(_wrapper_inst.serialize_to_bytes())
