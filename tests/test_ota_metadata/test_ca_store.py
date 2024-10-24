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

import shutil
import subprocess
from pathlib import Path

import pytest
from OpenSSL import crypto

from ota_metadata.utils.cert_store import (
    CACertStoreInvalid,
    load_ca_cert_chains,
    load_cert_in_pem,
)
from tests.conftest import TEST_DIR
from tests.conftest import TestConfiguration as cfg

GEN_CERTS_SCRIPT = TEST_DIR / "keys" / "gen_certs.sh"
TEST_BASE_SIGN_PEM = Path(cfg.CERTS_DIR) / "sign.pem"


@pytest.fixture
def setup_ca_chain(tmp_path: Path) -> tuple[str, Path, Path]:
    """Create the certs dir and generate certs.

    Returns:
        A tuple of chain prefix, sign cert path and certs dir.
    """
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy(GEN_CERTS_SCRIPT, certs_dir)
    gen_certs_script = certs_dir / GEN_CERTS_SCRIPT.name

    chain = "test_chain"
    subprocess.run(
        [
            "bash",
            str(gen_certs_script),
            chain,
        ],
        cwd=certs_dir,
    )
    return chain, certs_dir / "sign.pem", certs_dir


def test_ca_store(setup_ca_chain: tuple[str, Path, Path]):
    ca_chain, sign_pem, certs_dir = setup_ca_chain

    ca_store = load_ca_cert_chains(certs_dir)

    # verification should fail with wrong cert signed by other chain.
    with pytest.raises(crypto.X509StoreContextError):
        crypto.X509StoreContext(
            store=ca_store[ca_chain],
            certificate=load_cert_in_pem(TEST_BASE_SIGN_PEM.read_bytes()),
        ).verify_certificate()

    # verification should succeed with proper chain and corresponding sign cert.
    crypto.X509StoreContext(
        store=ca_store[ca_chain],
        certificate=load_cert_in_pem(sign_pem.read_bytes()),
    ).verify_certificate()


def test_ca_store_empty(tmp_path: Path):
    with pytest.raises(CACertStoreInvalid):
        load_ca_cert_chains(tmp_path)


def test_ca_store_invalid(tmp_path: Path):
    # create invalid certs under tmp_path
    root_cert = tmp_path / "test.root.pem"
    intermediate = tmp_path / "test.intermediate.pem"

    root_cert.write_text("abcdef")
    intermediate.write_text("123456")

    with pytest.raises(CACertStoreInvalid):
        load_ca_cert_chains(tmp_path)
