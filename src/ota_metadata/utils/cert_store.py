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
"""Implementation of otaclient CA cert chain store."""


from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, Iterable

from cryptography.x509 import Certificate, Name, load_pem_x509_certificate

from otaclient_common.typing import StrOrPath

logger = logging.getLogger(__name__)

# we search the CA chains with the following naming schema:
#   <env_name>.<intermediate|root>.pem,
#   in which <env_name> should match regex pattern `[\w\-]+`
# e.g:
#   dev chain: dev.intermediate.pem, dev.root.pem
#   prd chain: prd.intermediate.pem, prd.intermediate.pem
CERT_NAME_PA = re.compile(r"(?P<chain>[\w\-]+)\.[\w\-]+\.pem")


class CACertStoreInvalid(Exception): ...


class CACertChains(Dict[Name, Certificate]):
    """A dict that stores all the CA certs of a cert chains.

    The key is the cert's subject, value is the cert itself.
    """

    chain_prefix: str = ""

    def set_chain_prefix(self, prefix: str) -> None:
        self.chain_prefix = prefix

    def add_cert(self, cert: Certificate) -> None:
        self[cert.subject] = cert

    def add_certs(self, certs: Iterable[Certificate]) -> None:
        for cert in certs:
            self[cert.subject] = cert

    def verify(self, cert: Certificate) -> None:
        """Verify the input <cert> against this chain.

        Raises:
            ValueError on input cert is not signed by this chain.
            Other exceptions that could be raised by verify_directly_issued_by API.

        Returns:
            Return None on successful verification, otherwise raises exception.
        """
        _start = cert
        for _ in range(len(self) + 1):
            if _start.issuer == _start.subject:
                return

            _issuer = self[_start.issuer]
            _start.verify_directly_issued_by(_issuer)

            _start = _issuer
        raise ValueError(f"failed to verify {cert} against chain {self.chain_prefix}")


class CACertChainStore(Dict[str, CACertChains]):
    """A dict that stores CA chain name and CACertChains mapping."""


def load_ca_cert_chains(cert_dir: StrOrPath) -> CACertChainStore:
    """Load CA cert chains from <cert_dir>.

    Raises:
        CACertStoreInvalid on failed import.

    Returns:
        A dict
    """
    cert_dir = Path(cert_dir)

    ca_set_prefix = set()
    for cert in cert_dir.glob("*.pem"):
        if m := CERT_NAME_PA.match(cert.name):
            ca_set_prefix.add(m.group("chain"))
        else:
            _err_msg = f"detect invalid named certs: {cert.name}"
            logger.warning(_err_msg)

    if not ca_set_prefix:
        _err_msg = f"no CA cert chains found to be installed under the {cert_dir}!!!"
        logger.error(_err_msg)
        raise CACertStoreInvalid(_err_msg)

    logger.info(f"found installed CA chains: {ca_set_prefix}")

    ca_chains = CACertChainStore()
    for ca_prefix in sorted(ca_set_prefix):
        try:
            ca_chain = CACertChains()
            ca_chain.set_chain_prefix(ca_prefix)

            for c in cert_dir.glob(f"{ca_prefix}.*.pem"):
                _loaded_ca_cert = load_pem_x509_certificate(c.read_bytes())
                ca_chain.add_cert(_loaded_ca_cert)
            ca_chains[ca_prefix] = ca_chain
        except Exception as e:
            _err_msg = f"failed to load CA chain {ca_prefix}: {e!r}"
            logger.warning(_err_msg)

    if not ca_chains:
        _err_msg = "all found CA chains are invalid, no CA chain is imported!!!"
        logger.error(_err_msg)
        raise CACertStoreInvalid(_err_msg)

    logger.info(f"loaded CA cert chains: {list(ca_chains)}")
    return ca_chains
