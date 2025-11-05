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
"""Check BSP version compatibility."""

from __future__ import annotations

import logging
import time
from http import HTTPStatus
from typing import Optional
from urllib.parse import urlsplit

from otaclient.boot_control._jetson_uefi import JetsonUEFIBootControl
from otaclient.boot_control.configs import JetsonBootCommon
from otaclient.boot_control.protocol import BootControllerProtocol
from otaclient_common.downloader import DownloaderPool

logger = logging.getLogger(__name__)

BSP_VERSION_PATH = "data" + JetsonBootCommon.NV_TEGRA_RELEASE_FPATH
DOWNLOAD_TIMEOUT = 2
RETRY_TIMES = 12
RETRY_INTERVAL = 1  # second


def _download_bsp_version_file(
    base_url: str, *, downloader_pool: DownloaderPool
) -> Optional[str]:
    """Download BSP version file content."""
    logger.info("perform BSP version compatibility check ...")
    _downloader = downloader_pool.get_instance()
    try:
        _hint_file_url = f"{base_url.rstrip('/')}/{BSP_VERSION_PATH}"
        if _downloader._force_http:
            _hint_file_url = urlsplit(_hint_file_url)._replace(scheme="http").geturl()

        for _ in range(RETRY_TIMES):
            try:
                resp = _downloader._session.get(
                    _hint_file_url, timeout=DOWNLOAD_TIMEOUT
                )
                _status_code = resp.status_code
                if _status_code == HTTPStatus.OK:
                    return resp.text.strip()
                if _status_code in [HTTPStatus.UNAUTHORIZED, HTTPStatus.NOT_FOUND]:
                    return None
            except Exception:
                pass

            time.sleep(RETRY_INTERVAL)
        return None
    except Exception as e:
        logger.warning(f"unexpected failure during probing image version: {e}")
        return None
    finally:
        downloader_pool.release_instance()


# API function
def check_bsp_version_legacy(
    base_url: str,
    *,
    downloader_pool: DownloaderPool,
    boot_controller: BootControllerProtocol,
) -> bool:
    """Check BSP version compatibility for legacy OTA image."""

    # check if the boot controller is Jetson UEFI
    if not isinstance(boot_controller, JetsonUEFIBootControl):
        logger.info("BSP version check is skipped for non-Jetson UEFI bootloader.")
        return True

    # downloaded BSP version file content
    _download_bsp_version_file_content = _download_bsp_version_file(
        base_url, downloader_pool=downloader_pool
    )
    if _download_bsp_version_file_content is None:
        logger.info("BSP version file not found; skipping compatibility check.")
        return True  # Skip check if file not found

    # perform compatibility check
    return boot_controller.check_bsp_version_compatibility(
        _download_bsp_version_file_content
    )
