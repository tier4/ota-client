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

import pytest

from ota_metadata.file_table._types import FileEntryAttrs


@pytest.mark.parametrize(
    "_in",
    (
        # directory
        (
            FileEntryAttrs(
                mode=0o040755,
                uid=1000,
                gid=1000,
                xattrs={
                    "user.foo": "foo",
                    "user.bar": "bar",
                },
            )
        ),
        # normal file
        (
            FileEntryAttrs(
                mode=0o100644,
                uid=1000,
                gid=1000,
                size=12345,
                inode=67890,
                xattrs={
                    "user.foo": "foo",
                    "user.bar": "bar",
                },
            )
        ),
        # symlink file
        (
            FileEntryAttrs(
                mode=0o120777,
                uid=1000,
                gid=1000,
            )
        ),
    ),
)
def test_validator_and_serializer(_in: FileEntryAttrs) -> None:
    _serialized = _in._serializer()
    assert _in._validator(_serialized) == _in
