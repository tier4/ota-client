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
