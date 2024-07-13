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

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict
from typing_extensions import Self


class BaseFixedConfig(BaseModel):
    """Common base for configs that should be fixed and not changable."""

    model_config = ConfigDict(frozen=True, validate_default=True)


class ExtractAttrsMixin:
    """Extract string types attrs from an object."""

    def _extract_to_ns(self) -> Self:
        attrns = filter(lambda x: not x.startswith("_"), dir(self))
        res = {k: getattr(self, k) for k in attrns if isinstance(getattr(self, k), str)}
        return SimpleNamespace(**res)  # type: ignore


class DynamicRootMixin:

    _HOST_ROOTFS: Literal["/"] | str = "/"

    def __init__(self, host_rootfs: Literal["/"] | str = "/") -> None:
        self._HOST_ROOTFS = host_rootfs

    if not TYPE_CHECKING:

        def __getattribute__(self, name: str) -> str | Any:
            attr_value = super().__getattribute__(name)
            if name.startswith("_") or self._HOST_ROOTFS == "/":
                return attr_value

            # dynamically update the root according to HOST_ROOTFS value.
            attr_value = Path(attr_value).relative_to("/")
            return str(self._HOST_ROOTFS / attr_value)
