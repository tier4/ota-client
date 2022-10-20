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
from dataclasses import dataclass
from typing import Any, Callable, Dict, Protocol

from otaclient.app.downloader import Downloader
from ..ota_metadata import OTAMetadata
from ..proto import wrapper
from ..update_stats import OTAUpdateStatsCollector


@dataclass
class UpdateMeta:
    """Meta info for standby slot creator to update slot."""

    cookies: Dict[str, Any]  # cookies needed for requesting remote ota files
    metadata: OTAMetadata  # meta data for the update request
    url_base: str  # base url of the remote ota image
    boot_dir: str  # where to populate files under /boot
    standby_slot_mount_point: str
    ref_slot_mount_point: str


class StandbySlotCreatorProtocol(Protocol):
    """Protocol that describes standby slot creating mechanism.
    Attrs:
        cookies: authentication cookies used by ota_client to fetch files from the remote ota server.
        metadata: metadata of the requested ota image.
        url_base: base url that ota image located.
        new_root: the root folder of bank to be updated.
        reference_root: the root folder to copy from.
        status_tracker: pass real-time update stats to ota-client.
        status_updator: inform which ota phase now.
    """

    stats_collector: OTAUpdateStatsCollector
    update_phase_tracker: Callable[[wrapper.StatusProgressPhase], None]

    def __init__(
        self,
        update_meta: UpdateMeta,
        stats_collector: OTAUpdateStatsCollector,
        update_phase_tracker: Callable[[wrapper.StatusProgressPhase], None],
        downloader: Downloader,
    ) -> None:
        ...

    @abstractmethod
    def create_standby_slot(self):
        ...

    @classmethod
    @abstractmethod
    def should_erase_standby_slot(cls) -> bool:
        """Tell whether standby slot should be erased
        under this standby slot creating mode."""

    @classmethod
    @abstractmethod
    def is_standby_as_ref(cls) -> bool:
        """Tell whether the slot creator intends to use
        in-place update."""
