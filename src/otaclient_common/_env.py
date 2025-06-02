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

import functools
import os
from typing import Optional

from otaclient.configs.cfg import cfg


def is_dynamic_client_running() -> bool:
    """Check if the dynamic client is running."""
    return bool(os.getenv(cfg.RUNNING_DOWNLOADED_DYNAMIC_OTA_CLIENT))


@functools.lru_cache(maxsize=None)
def get_dynamic_client_chroot_path() -> Optional[str]:
    """Get the chroot path."""
    if is_dynamic_client_running():
        return cfg.ACTIVE_SLOT_MNT
    return None
