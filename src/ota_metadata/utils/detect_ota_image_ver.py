from __future__ import annotations

import logging
import time
from http import HTTPStatus
from urllib.parse import urlsplit

from otaclient_common.downloader import DownloaderPool

logger = logging.getLogger(__name__)

OTA_IMAGE_V1_HINT = "oci-layout"
DOWNLOAD_TIMEOUT = 2  # hint file only takes 31bytes
RETRY_TIMES = 3


def check_if_ota_image_v1(base_url: str, *, downloader_pool: DownloaderPool) -> bool:
    """Determine whether an OTA image host is OTA Image v1."""
    _downloader = downloader_pool.get_instance()
    try:
        _hint_file_url = f"{base_url.rstrip('/')}/{OTA_IMAGE_V1_HINT}"
        if _downloader._force_http:
            _hint_file_url = urlsplit(_hint_file_url)._replace(scheme="http").geturl()

        for _ in range(RETRY_TIMES):
            try:
                resp = _downloader._session.get(
                    _hint_file_url, timeout=DOWNLOAD_TIMEOUT
                )
                if resp.status_code in [HTTPStatus.OK]:
                    return True
            except Exception:
                pass

            time.sleep(1)
        return False
    except Exception as e:
        logger.warning(f"unexpected failure during probing image version: {e}")
        return False
    finally:
        downloader_pool.release_instance()
