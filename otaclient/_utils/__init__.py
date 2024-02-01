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
import os.path
from functools import cached_property
from pydantic import computed_field
from typing import Any, Callable, TypeVar

T = TypeVar("T")

_CONTAINER_INDICATOR_FILES = [
    "/.dockerenv",
    "/run/.dockerenv",
    "/run/.containerenv",
]


def if_run_as_container() -> bool:
    for indicator in _CONTAINER_INDICATOR_FILES:
        if os.path.isfile(indicator):
            return True
    return False


def cached_computed_field(_f: Callable[[Any], Any]) -> cached_property[Any]:
    return computed_field(cached_property(_f))


def chain_query(_obj: dict[str, Any], _path: str, *, splitter: str = ":") -> Any:
    """Chain access a nested dict <_obj> according to search <_path>.

    For example:
        for <_obj> as a dict object like the following:
        _obj = {
            "level_name": "root_level",
            "level1": {
                "level_name": "level1,
                "level2": {
                    "level_name": "level2",
                    "attr_we_need": "some_value",
                }
            }
        }

        To get the <attr_we_need>, we can use the search path as follow:
            level1:level2:attr_we_need

        then we can call this method like:
        cahin_query(_obj, "level1:level2:attr_we_need") to get the value we need.

    Args:
        _obj: a nested dict object.
        _path: a dot-splitted search path string.
    """
    for _next in _path.split(splitter):
        _obj = _obj[_next]
    return _obj
