from __future__ import annotations

import logging
import time
from http import HTTPStatus
from urllib.parse import urlsplit

from ota_image_libs.v1.consts import IMAGE_INDEX_FNAME
from ota_image_libs.v1.image_index.schema import ImageIndex
from ota_image_libs.v1.image_manifest.schema import ImageIdentifier

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
                if _status_code in [
                    HTTPStatus.UNAUTHORIZED,
                    HTTPStatus.FORBIDDEN,
                    HTTPStatus.NOT_FOUND,
                ]:
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


INDEX_DOWNLOAD_TIMEOUT = 16


def has_parameter_set_for(
    base_url: str, image_id: ImageIdentifier, *, downloader_pool: DownloaderPool
) -> bool:
    """Whether the OTA image v1 at <base_url> carries a parameter set as the payload
    of <image_id>, from its `index.json`.

    NOTE: the index is not verified here: this only picks the updater, and the
        parameter set updater verifies it, as every v1 updater does, before it
        believes anything in it. Any failure here is a False, leaving the image to
        the updater it would have gone to anyway.
    """
    _downloader = downloader_pool.get_instance()
    try:
        _index_url = f"{base_url.rstrip('/')}/{IMAGE_INDEX_FNAME}"
        if _downloader._force_http:
            _index_url = urlsplit(_index_url)._replace(scheme="http").geturl()
        for _ in range(RETRY_TIMES):
            try:
                resp = _downloader._session.get(
                    _index_url, timeout=INDEX_DOWNLOAD_TIMEOUT
                )
                if resp.status_code == HTTPStatus.OK:
                    _index = ImageIndex.parse_metafile(resp.text)
                    return _index.find_parameter_set(image_id) is not None
            except Exception as e:
                logger.debug(f"failed to read the index for its payload kind: {e!r}")
            time.sleep(RETRY_INTERVAL)
        return False
    except Exception as e:
        logger.warning(f"unexpected failure when reading the payload kind: {e}")
        return False
    finally:
        downloader_pool.release_instance()
