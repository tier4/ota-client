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
"""Local helper: structural compare for protobuf messages.

Direct `==` comparison treats unset and explicitly-empty composite
fields (e.g. `Duration`) differently. This recursive comparator uses
`__slots__` so both forms compare equal.
"""

from __future__ import annotations

from google.protobuf.message import Message as _Message


def compare_message(left, right):
    if (_proto_class := type(left)) is not type(right):
        raise TypeError(f"{type(left)=} != {type(right)=}")

    for _attrn in _proto_class.__slots__:
        _attrv_l, _attrv_r = getattr(left, _attrn), getattr(right, _attrn)
        assert type(_attrv_l) is type(_attrv_r), f"compare failed on {_attrn=}"

        if isinstance(_attrv_l, _Message):
            compare_message(_attrv_l, _attrv_r)
        else:
            assert _attrv_l == _attrv_r, f"mismatch {_attrv_l=}, {_attrv_r=}"
