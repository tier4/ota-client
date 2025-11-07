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
import sys
from typing import Optional

from otaclient.configs.cfg import cfg
from otaclient_common import replace_root

try:
    cache = functools.cache  # type: ignore[attr-defined]
except AttributeError:
    cache = functools.lru_cache(maxsize=None)

RUN_AS_PYINSTALLER_BUNDLE = getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


@cache
def is_running_as_app_image() -> bool:
    """Check if is the systemd managed client running."""
    return bool(os.getenv(cfg.RUNNING_AS_APP_IMAGE))


@cache
def is_running_as_downloaded_dynamic_app() -> bool:
    """Check if is the downloaded dynamic client running."""
    return bool(os.getenv(cfg.RUNNING_DOWNLOADED_DYNAMIC_OTA_CLIENT))


@cache
def is_dynamic_client_running() -> bool:
    """Check if the dynamic client is running."""
    return is_running_as_app_image() or is_running_as_downloaded_dynamic_app()


@cache
def get_dynamic_client_chroot_path() -> Optional[str]:
    """Get the chroot path."""
    if is_dynamic_client_running():
        return cfg.DYNAMIC_CLIENT_MNT_HOST_ROOT
    return None


@cache
def get_otaclient_squashfs_download_dst() -> str:
    """Get the location to hold downloaded otaclient squashfs image."""
    if is_dynamic_client_running():
        return replace_root(
            cfg.DYNAMIC_CLIENT_SQUASHFS_FILE,
            cfg.CANONICAL_ROOT,
            cfg.DYNAMIC_CLIENT_MNT_HOST_ROOT,
        )
    return cfg.DYNAMIC_CLIENT_SQUASHFS_FILE
