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

from ota_metadata.legacy2.metadata import MetadataJWTParser
from ota_metadata.utils.cert_store import load_ca_cert_chains
from tests.conftest import CERTS_DIR, OTA_IMAGE_DIR

METADATA_JWT = OTA_IMAGE_DIR / "metadata.jwt"
SIGN_CERT = OTA_IMAGE_DIR / "certificate.pem"


def test_e2e() -> None:
    metadata_jwt = METADATA_JWT.read_text()
    sign_cert = SIGN_CERT.read_bytes()
    ca_store = load_ca_cert_chains(CERTS_DIR)

    parser = MetadataJWTParser(
        metadata_jwt,
        ca_chains_store=ca_store,
    )

    # step1: verify sign cert against CA store
    parser.verify_metadata_cert(sign_cert)
    # step2: verify metadata.jwt against sign cert
    parser.verify_metadata_signature(sign_cert)
