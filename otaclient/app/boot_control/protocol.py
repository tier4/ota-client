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


from abc import abstractmethod
from pathlib import Path
from typing import Generator, Protocol

from ..proto import wrapper


class BootControllerProtocol(Protocol):
    """Boot controller protocol for otaclient."""

    @abstractmethod
    def get_ota_status(self) -> wrapper.StatusOta:
        """Get the stored ota_status of current active slot."""

    @abstractmethod
    def get_standby_slot_path(self) -> Path:
        """Get the Path points to the standby slot mount point."""

    @abstractmethod
    def get_standby_boot_dir(self) -> Path:
        """Get the Path points to the standby boot folder."""

    @abstractmethod
    def pre_update(self, version: str, *, standby_as_ref: bool, erase_standby: bool):
        ...

    @abstractmethod
    def pre_rollback(self):
        ...

    @abstractmethod
    def post_update(self) -> Generator[None, None, None]:
        ...

    @abstractmethod
    def post_rollback(self):
        ...

    @abstractmethod
    def load_version(self) -> str:
        """Read the version info from the current slot."""

    @abstractmethod
    def on_operation_failure(self):
        ...
