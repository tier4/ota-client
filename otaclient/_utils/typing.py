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
import ipaddress
from pathlib import Path
from typing import Any, Callable, TypeVar, Union
from typing_extensions import Concatenate, ParamSpec
from urllib.parse import urlparse

P = ParamSpec("P")
T = TypeVar("T")
RT = TypeVar("RT")

StrOrPath = Union[str, Path]


def copy_callable_typehint(_source: Callable[P, Any]):
    """This helper function return a decorator that can type hint the target
    function as the _source function.

    At runtime, this decorator actually does nothing, but just return the input function as it.
    But the returned function will have the same type hint as the source function in ide.
    It will not impact the runtime behavior of the decorated function.
    """

    def _decorator(target) -> Callable[P, Any]:
        return target

    return _decorator


def copy_callable_typehint_to_method(_source: Callable[P, Any]):
    """Works the same as copy_callable_typehint, but omit the first arg."""

    def _decorator(target: Callable[..., RT]) -> Callable[Concatenate[Any, P], RT]:
        return target  # type: ignore

    return _decorator


def check_port(_in: Any) -> bool:
    return isinstance(_in, int) and _in >= 0 and _in <= 65535


# pydantic helpers


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


def port_validator(value: int) -> int:
    assert value >= 0 and value <= 65535, f"invalid port number: {value}"
    return value


def ip_validator(value: str) -> str:
    ipaddress.ip_address(value)
    return value


def http_url_validator(value: str) -> str:
    _parsed = urlparse(value)
    assert _parsed.scheme in ("http", "https"), f"{value} is not a http(s) URL"
    assert _parsed.netloc, f"{value} doesn't contain netloc"
    return value
