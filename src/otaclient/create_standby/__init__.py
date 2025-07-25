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


from otaclient.create_standby.delta_gen import (
    InPlaceDeltaGenFullDiskScan,
    InPlaceDeltaWithBaseFileTable,
    RebuildDeltaGenFullDiskScan,
    RebuildDeltaWithBaseFileTable,
)
from otaclient.create_standby.update_slot import UpdateStandbySlot
from otaclient.create_standby.utils import can_use_in_place_mode

__all__ = [
    "can_use_in_place_mode",
    "UpdateStandbySlot",
    "InPlaceDeltaGenFullDiskScan",
    "InPlaceDeltaWithBaseFileTable",
    "RebuildDeltaGenFullDiskScan",
    "RebuildDeltaWithBaseFileTable",
]
