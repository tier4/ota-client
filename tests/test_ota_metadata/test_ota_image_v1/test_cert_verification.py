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
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.x509 import load_pem_x509_certificate

from ota_metadata.utils.cert_store import load_ca_store

logger = logging.getLogger(__name__)

CERT_GEN_SCRIPT = Path(__file__).parent.parent.parent / "keys" / "gen_certs.sh"
CHAINS = ["dev", "stg", "prd"]


@pytest.fixture
def gen_ca_chains(tmp_path: Path) -> tuple[Path, Path, Path]:
    """
    Check tests/keys/gen_certs.sh for more details.
    """
    _script = tmp_path / "gen_certs.sh"
    _ca_dir = tmp_path / "root_ca"
    _interm_dir = tmp_path / "interm_ca"
    _certs_dir = tmp_path / "certs"

    _ca_dir.mkdir()
    _interm_dir.mkdir()
    _certs_dir.mkdir()

    shutil.copy(CERT_GEN_SCRIPT, _script)
    os.chmod(_script, 0o750)
    for chain in CHAINS:
        subprocess.run([str(_script), chain], check=True, capture_output=True)
        for _k in tmp_path.glob("*.key"):
            _k.unlink()
        shutil.move(f"{chain}.root.pem", _ca_dir)
        shutil.move(f"{chain}.interm.pem", _interm_dir)
        shutil.move("sign.pem", _certs_dir / f"{chain}.sign.pem")

    logger.info(f"CA {CHAINS} generated")
    return _ca_dir, _interm_dir, _certs_dir


def test_ca_store_map(gen_ca_chains: tuple[Path, Path, Path]):
    _ca_dir, _interm_dir, _certs_dir = gen_ca_chains
    ca_stores = load_ca_store(_ca_dir)

    for chain in CHAINS:
        _cert = load_pem_x509_certificate(
            (_certs_dir / f"{chain}.sign.pem").read_bytes()
        )
        _interm = load_pem_x509_certificate(
            (_interm_dir / f"{chain}.interm.pem").read_bytes()
        )
        ca_stores.verify(_cert, interm_cas=[_interm])
