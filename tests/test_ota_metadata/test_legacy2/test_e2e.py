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
import os
from hashlib import sha256
from pathlib import Path
from queue import Queue

from pytest_mock import MockerFixture

from ota_metadata.legacy2.metadata import MetadataJWTParser
from ota_metadata.utils.cert_store import load_ca_cert_chains
from otaclient._types import MultipleECUStatusFlags
from otaclient._utils import SharedOTAClientMetricsReader
from otaclient.metrics import OTAMetricsData
from otaclient.ota_core._updater_base import LegacyOTAImageSupportMixin
from otaclient_common.downloader import DownloaderPool
from tests.conftest import (
    CERTS_DIR,
    OTA_IMAGE_DIR,
    OTA_IMAGE_SIGN_CERT,
    cfg,
)
from tests.test_ota_metadata.conftest import iter_helper

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


def test_download_and_parse_metadata(tmp_path: Path, mocker: MockerFixture):
    legacy_ota_image = LegacyOTAImageSupportMixin(
        version="dummy_version",
        raw_url_base=cfg.OTA_IMAGE_URL,
        session_wd=tmp_path,
        downloader_pool=DownloaderPool(instance_num=3, hash_func=sha256),
        session_id=f"session_id_{os.urandom(2).hex()}",
        ecu_status_flags=mocker.MagicMock(spec=MultipleECUStatusFlags),
        status_report_queue=mocker.MagicMock(spec=Queue),
        metrics=mocker.MagicMock(spec=OTAMetricsData),
        shm_metrics_reader=mocker.MagicMock(spec=SharedOTAClientMetricsReader),
    )  # type: ignore

    ca_chains_store = load_ca_cert_chains(cfg.CERTS_DIR)
    legacy_ota_image.setup_ota_image_support(ca_chains_store=ca_chains_store)
    legacy_ota_image._process_metadata()

    # ------ check result ------ #
    ota_metadata = legacy_ota_image._ota_metadata
    fst_helper = ota_metadata.file_table_helper
    assert iter_helper(fst_helper.iter_dir_entries()) == ota_metadata.total_dirs_num
    # NOTE: for legacy OTA image, non_regular_files catagory only has symlink
    assert (
        iter_helper(fst_helper.iter_non_regular_entries())
        == ota_metadata.total_symlinks_num
    )
    assert (
        iter_helper(fst_helper.iter_regular_entries())
        == ota_metadata.total_regulars_num
    )
