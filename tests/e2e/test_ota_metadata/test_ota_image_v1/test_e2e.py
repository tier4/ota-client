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
"""End-to-end tests for OTA image v1 metadata loading against `/ota-image_v1`."""

from __future__ import annotations

import datetime
import logging
import os
from hashlib import sha256
from pathlib import Path
from queue import Queue

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from ota_image_libs.v1.image_manifest.schema import ImageIdentifier, OTAReleaseKey
from pytest_mock import MockerFixture

from ota_metadata.utils.cert_store import load_ca_store
from otaclient import errors as ota_errors
from otaclient._types import MultipleECUStatusFlags
from otaclient._utils import SharedOTAClientMetricsReader
from otaclient.metrics import OTAMetricsData
from otaclient.ota_core._updater_base import OTAImageV1SupportMixin
from otaclient_common.downloader import DownloaderPool
from tests.conftest import CERTS_OTA_IMAGE_V1_DIR
from tests.e2e.test_ota_metadata.conftest import iter_helper

logger = logging.getLogger(__name__)


def _ota_image_v1_support(
    session_wd: Path, mocker: MockerFixture, url_base: str
) -> OTAImageV1SupportMixin:
    # NOTE: directly bootstrap the mixin as we only check metadata downloading and parsing
    return OTAImageV1SupportMixin(
        version="dummy_version",
        raw_url_base=url_base,
        session_wd=session_wd,
        downloader_pool=DownloaderPool(instance_num=3, hash_func=sha256),
        session_id=f"session_id_{os.urandom(2).hex()}",
        ecu_status_flags=mocker.MagicMock(spec=MultipleECUStatusFlags),
        status_report_queue=mocker.MagicMock(spec=Queue),
        metrics=mocker.MagicMock(spec=OTAMetricsData),
        shm_metrics_reader=mocker.MagicMock(spec=SharedOTAClientMetricsReader),
    )  # type: ignore


def _gen_self_signed_root_ca(dst: Path) -> None:
    _key = ec.generate_private_key(ec.SECP256R1())
    _name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "untrusted.root")])
    _now = datetime.datetime.now(datetime.timezone.utc)
    _cert = (
        x509.CertificateBuilder()
        .subject_name(_name)
        .issuer_name(_name)
        .public_key(_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now - datetime.timedelta(days=1))
        .not_valid_after(_now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(_key, hashes.SHA256())
    )
    dst.write_bytes(_cert.public_bytes(serialization.Encoding.PEM))


def test_download_and_parse_metadata(
    tmp_path: Path,
    mocker: MockerFixture,
    ota_image_v1_server: str,
) -> None:
    # ------ execution ------ #
    ota_image_v1 = _ota_image_v1_support(tmp_path, mocker, ota_image_v1_server)

    ca_store = load_ca_store(CERTS_OTA_IMAGE_V1_DIR)
    ota_image_v1.setup_ota_image_support(
        ca_store=ca_store,
        image_identifier=ImageIdentifier("autoware", OTAReleaseKey.dev),
    )
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


def test_untrusted_sign_cert(
    tmp_path: Path,
    mocker: MockerFixture,
    ota_image_v1_server: str,
) -> None:
    """index.jwt signed by a cert not issued by any of the trusted CAs is rejected."""
    _untrusted_certs_dir = tmp_path / "untrusted_certs"
    _untrusted_certs_dir.mkdir()
    _gen_self_signed_root_ca(_untrusted_certs_dir / "dev.root.pem")

    _session_wd = tmp_path / "session_wd"
    _session_wd.mkdir()
    ota_image_v1 = _ota_image_v1_support(_session_wd, mocker, ota_image_v1_server)
    ota_image_v1.setup_ota_image_support(
        ca_store=load_ca_store(_untrusted_certs_dir),
        image_identifier=ImageIdentifier("autoware", OTAReleaseKey.dev),
    )

    with pytest.raises(ota_errors.MetadataJWTVerficationFailed):
        ota_image_v1._process_metadata()
    assert ota_image_v1._ota_image_helper.image_index is None
