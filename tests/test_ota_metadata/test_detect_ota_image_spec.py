from __future__ import annotations

from hashlib import sha256

from ota_metadata.utils.detect_ota_image_ver import check_if_ota_image_v1
from otaclient_common.downloader import DownloaderPool
from tests.conftest import cfg


def test_detect_ota_image_ver():
    _downloader = DownloaderPool(instance_num=3, hash_func=sha256)
    assert not check_if_ota_image_v1(cfg.OTA_IMAGE_URL, downloader_pool=_downloader)
    assert check_if_ota_image_v1(cfg.OTA_IMAGE_V1_URL, downloader_pool=_downloader)
