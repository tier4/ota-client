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
r"""create_standby package provide the feature of applying update to standby slot.

This package has two main jobs:
1. calculate and prepare delta against target image and local running image
2. applying changes to the standby slot to update standby slot to target image

The flow of this package working is as follow:
1. upper caller(otaclient) downloads the OTA image metadata files,
2. upper caller(otaclient) prepare the standby slot device(erase it if the implemented module
    requires it),
2. upper caller(otaclient) calls the calculate_and_prepare_delta method of
    this package to retrieve the delta(and prepare the local copies),
3. upper caller(otaclient) downloads the needed OTA image files(files that
    don't present locally),
4. upper caller(otaclient) calls the create_standby_slot method to apply
    update to the standby slot.
"""


from __future__ import annotations

from abc import abstractmethod
from queue import Queue
from typing import Protocol

from ota_metadata.legacy.parser import OTAMetadata
from otaclient.status_monitor import StatusReport

from .common import DeltaBundle


class StandbySlotCreatorProtocol(Protocol):
    """Protocol that describes standby slot creating mechanism."""

    def __init__(
        self,
        *,
        ota_metadata: OTAMetadata,
        boot_dir: str,
        standby_slot_mount_point: str,
        active_slot_mount_point: str,
        status_report_queue: Queue[StatusReport],
        session_id: str,
    ) -> None: ...

    @abstractmethod
    def create_standby_slot(self): ...

    @abstractmethod
    def calculate_and_prepare_delta(self) -> DeltaBundle: ...

    @classmethod
    @abstractmethod
    def should_erase_standby_slot(cls) -> bool:
        """Tell whether standby slot should be erased
        under this standby slot creating mode."""
