from __future__ import annotations

import logging
from hashlib import sha256

from ota_metadata.utils.detect_ota_image_ver import check_if_ota_image_v1
from otaclient_common.downloader import DownloaderPool
from tests.conftest import cfg

logger = logging.getLogger(__name__)


def test_detect_ota_image_ver():
    _downloader = DownloaderPool(instance_num=3, hash_func=sha256)

    _check_res1 = check_if_ota_image_v1(cfg.OTA_IMAGE_URL, downloader_pool=_downloader)
    logger.info(
        f"check if {cfg.OTA_IMAGE_URL=} hosts OTA image v1: {_check_res1}(should be False)"
    )
    assert not _check_res1

    _check_res2 = check_if_ota_image_v1(
        cfg.OTA_IMAGE_V1_URL, downloader_pool=_downloader
    )
    logger.info(
        f"check if {cfg.OTA_IMAGE_V1_URL=} hosts OTA image v1: {_check_res2}(should be True)"
    )
    assert _check_res2
