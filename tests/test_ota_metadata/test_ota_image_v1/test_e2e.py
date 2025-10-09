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

from __future__ import annotations

import logging
import os
from hashlib import sha256
from pathlib import Path
from queue import Queue

from pytest_mock import MockerFixture

from ota_metadata.utils.cert_store import load_ca_store
from otaclient._types import MultipleECUStatusFlags
from otaclient._utils import SharedOTAClientMetricsReader
from otaclient.metrics import OTAMetricsData
from otaclient.ota_core._updater_base import OTAImageV1SupportMixin
from otaclient_common.downloader import DownloaderPool
from tests.conftest import cfg
from tests.test_ota_metadata.conftest import iter_helper

logger = logging.getLogger(__name__)


def test_download_and_parse_metadata(tmp_path: Path, mocker: MockerFixture):
    # ------ execution ------ #
    # NOTE: directly bootstrap the mixin as we only check metadata downloading and parsing
    ota_image_v1 = OTAImageV1SupportMixin(
        version="dummy_version",
        raw_url_base=cfg.OTA_IMAGE_V1_URL,
        session_wd=tmp_path,
        downloader_pool=DownloaderPool(instance_num=3, hash_func=sha256),
        session_id=f"session_id_{os.urandom(2).hex()}",
        ecu_status_flags=mocker.MagicMock(spec=MultipleECUStatusFlags),
        status_report_queue=mocker.MagicMock(spec=Queue),
        metrics=mocker.MagicMock(spec=OTAMetricsData),
        shm_metrics_reader=mocker.MagicMock(spec=SharedOTAClientMetricsReader),
    )  # type: ignore

    ca_store = load_ca_store(cfg.CERTS_OTA_IMAGE_V1_DIR)
    ota_image_v1.setup_ota_image_support(ca_store=ca_store)
    ota_image_v1._process_metadata()

    # ------ check result ------ #
    ota_image_helper = ota_image_v1._ota_image_helper
    assert (_image_index := ota_image_helper.image_index)
    assert (_image_manifest := ota_image_helper.image_manifest)
    assert (_image_config := ota_image_helper.image_config)
    logger.info(str(_image_index))
    logger.info(str(_image_manifest))
    logger.info(str(_image_config))

    fst_helper = ota_image_helper.file_table_helper
    assert (
        iter_helper(fst_helper.iter_dir_entries())
        == _image_config.labels.sys_image_dirs_count
    )
    assert (
        iter_helper(fst_helper.iter_non_regular_entries())
        == _image_config.labels.sys_image_non_regular_files_count
    )
    assert (
        iter_helper(fst_helper.iter_regular_entries())
        == _image_config.sys_image_regular_files_count
    )
