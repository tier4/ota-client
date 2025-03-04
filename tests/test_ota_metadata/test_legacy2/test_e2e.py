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
"""Test OTA metadata loading with OTA image within the test container."""


from __future__ import annotations

import logging
import threading
from functools import partial
from hashlib import sha256
from pathlib import Path

import pytest

from ota_metadata.legacy2.metadata import MetadataJWTParser, OTAMetadata
from ota_metadata.utils.cert_store import load_ca_cert_chains
from otaclient_common.downloader import DownloaderPool, DownloadInfo
from otaclient_common.retry_task_map import ThreadPoolExecutorWithRetry
from tests.conftest import CERTS_DIR, OTA_IMAGE_DIR, OTA_IMAGE_SIGN_CERT, OTA_IMAGE_URL

METADATA_JWT = OTA_IMAGE_DIR / "metadata.jwt"

logger = logging.getLogger(__name__)


def test_metadata_jwt_parser_e2e() -> None:
    metadata_jwt = METADATA_JWT.read_text()
    sign_cert = OTA_IMAGE_SIGN_CERT.read_bytes()
    ca_store = load_ca_cert_chains(CERTS_DIR)

    parser = MetadataJWTParser(
        metadata_jwt,
        ca_chains_store=ca_store,
    )

    # step1: verify sign cert against CA store
    parser.verify_metadata_cert(sign_cert)
    # step2: verify metadata.jwt against sign cert
    parser.verify_metadata_signature(sign_cert)


class TestFullE2E:

    @pytest.fixture(autouse=True)
    def setup_test(self):
        self.downloader = DownloaderPool(
            instance_num=1,
            hash_func=sha256,
            chunk_size=1024**2,
        )

    def _download_to_thread(
        self, _download_info_list: list[DownloadInfo], *, condition: threading.Condition
    ):
        _downloader_in_thread = self.downloader.get_instance()
        with condition:
            for _download_info in _download_info_list:
                _downloader_in_thread.download(
                    url=_download_info.url,
                    dst=_download_info.dst,
                    digest=_download_info.digest,
                )
            condition.notify()

    def test_full_e2e(self, tmp_path: Path):
        ota_metadata_parser = OTAMetadata(
            base_url=OTA_IMAGE_URL,
            session_dir=tmp_path,
            ca_chains_store=load_ca_cert_chains(CERTS_DIR),
        )

        _condition = threading.Condition()
        _metadata_processor = ota_metadata_parser.download_metafiles(_condition)

        with ThreadPoolExecutorWithRetry(max_concurrent=6, max_workers=3) as mapper:
            for _fut in mapper.ensure_tasks(
                partial(self._download_to_thread, condition=_condition),
                _metadata_processor,
            ):
                if _exc := _fut.exception():
                    logger.exception(f"download failed: {_exc}")
                    _metadata_processor.throw(_exc)
                    raise _exc from None
