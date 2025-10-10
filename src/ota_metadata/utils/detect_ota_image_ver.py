from __future__ import annotations

import logging
import time
from http import HTTPStatus
from urllib.parse import urlsplit

from otaclient_common.downloader import DownloaderPool

logger = logging.getLogger(__name__)

OTA_IMAGE_V1_HINT = "oci-layout"
DOWNLOAD_TIMEOUT = 2  # hint file only takes 31bytes
RETRY_TIMES = 12
RETRY_INTERVAL = 1  # second


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
                _status_code = resp.status_code
                if _status_code == HTTPStatus.OK:
                    return True
                # NOTE(20251010): note that cloudfront endpoint will return 403 on non-existed path.
                if _status_code in [HTTPStatus.UNAUTHORIZED, HTTPStatus.NOT_FOUND]:
                    return False
            except Exception:
                pass

            time.sleep(RETRY_INTERVAL)
        return False
    except Exception as e:
        logger.warning(f"unexpected failure during probing image version: {e}")
        return False
    finally:
        downloader_pool.release_instance()
