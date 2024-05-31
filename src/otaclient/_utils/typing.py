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

from enum import Enum
from pathlib import Path
from typing import Any, Callable, TypeVar, Union

from pydantic import Field
from typing_extensions import Annotated, ParamSpec

P = ParamSpec("P")
T = TypeVar("T", bound=Enum)
RT = TypeVar("RT")

StrOrPath = Union[str, Path]

# pydantic helpers

NetworkPort = Annotated[int, Field(ge=1, le=65535)]


def gen_strenum_validator(enum_type: type[T]) -> Callable[[T | str | Any], T]:
    """A before validator generator that converts input value into enum
    before passing it to pydantic validator.

    NOTE(20240129): as upto pydantic v2.5.3, (str, Enum) field cannot
                    pass strict validation if input is str.
    """

    def _inner(value: T | str | Any) -> T:
        assert isinstance(
            value, (enum_type, str)
        ), f"{value=} should be {enum_type} or str type"
        return enum_type(value)

    return _inner
