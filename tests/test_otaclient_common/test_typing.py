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

from otaclient_common.typing import StrEnum


class EnumForTest(StrEnum):
    A = "A"


def test_str_enum():
    # str enum should be able to compare with string instance directly.
    assert EnumForTest.A == EnumForTest.A.value
    # str enum's __format__ should be the str's one, returning the str value.
    assert f"{EnumForTest.A}" == EnumForTest.A.value
    # our version of str enum for < 3.11 fully aligns with >= 3.11, which __str__
    #   is the str type's one.
    assert str(EnumForTest.A) == EnumForTest.A.value
