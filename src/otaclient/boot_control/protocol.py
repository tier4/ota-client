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

from abc import abstractmethod
from pathlib import Path
from typing import Protocol

from otaclient._types import OTAStatus


class BootControllerProtocol(Protocol):
    """Boot controller protocol for otaclient."""

    @abstractmethod
    def get_booted_ota_status(self) -> OTAStatus:
        """Get the ota_status loaded from status file during otaclient starts up.

        This value is meant to be used only once during otaclient starts up,
            to init the live_ota_status maintained by otaclient.
        """

    @abstractmethod
    def get_standby_slot_path(self) -> Path:
        """Get the Path points to the standby slot mount point."""

    @property
    @abstractmethod
    def bootloader_type(self) -> str:
        """The type of bootloader."""

    @property
    @abstractmethod
    def standby_slot_dev(self) -> Path:
        """The device of the standby slot."""

    @abstractmethod
    def get_standby_slot_dev(self) -> str:
        """Get the dev to the standby slot."""

    @abstractmethod
    def load_version(self) -> str:
        """Read the version info from the current slot."""

    @abstractmethod
    def load_standby_slot_version(self) -> str:
        """Read the version info from the standby slot."""

    @abstractmethod
    def on_operation_failure(self) -> None:
        """Cleanup by boot_control implementation when OTA failed."""

    #
    # ------ update ------ #
    #

    @abstractmethod
    def pre_update(self, *, standby_as_ref: bool, erase_standby: bool): ...

    @abstractmethod
    def post_update(self, update_version: str) -> None: ...

    @abstractmethod
    def finalizing_update(self, *, chroot: str | None = None) -> None:
        """Normally this method only reboots the device."""
