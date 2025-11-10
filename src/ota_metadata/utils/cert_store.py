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
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable

from cryptography.x509 import Certificate, Name, load_pem_x509_certificate
from ota_image_libs._crypto.x509_utils import CACertStore

from otaclient_common._typing import StrOrPath

logger = logging.getLogger(__name__)

# we search the CA chains with the following naming schema:
#   <env_name>.<intermediate|root>.pem,
#   in which <env_name> should match regex pattern `[\w\-]+`
# e.g:
#   dev chain: dev.intermediate.pem, dev.root.pem
#   prd chain: prd.intermediate.pem, prd.intermediate.pem
CERT_NAME_PA = re.compile(r"(?P<chain>[\w\-]+)\.[\w\-]+\.pem")


class CACertStoreInvalid(Exception): ...


class CAChain(Dict[Name, Certificate]):
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

    def internal_check(self) -> None:
        """Do an internal check to see if this CACertChain is valid.

        Currently one check will be performed:
        1. at least one root cert should be presented in the store.

        Raises:
            ValueError on failed check.
        """
        for _, cert in self.items():
            if cert.issuer == cert.subject:
                return
        raise ValueError("invalid chain: no root cert is presented")

    def verify(self, cert: Certificate) -> None:
        """Verify the input <cert> against this chain.

        Raises:
            ValueError on input cert is not signed by this chain.
            Other exceptions that could be raised by verify_directly_issued_by API.

        Returns:
            Return None on successful verification, otherwise raises exception.
        """
        _now = datetime.now(tz=timezone.utc)
        if not (cert.not_valid_after_utc >= _now >= cert.not_valid_before_utc):
            _err_msg = (
                "cert is not within valid period: "
                f"{cert.not_valid_after_utc=}, {_now=}, {cert.not_valid_before_utc=}"
            )
            raise ValueError(_err_msg)

        _start = cert
        for _ in range(len(self) + 1):
            if _start.issuer == _start.subject:
                return

            _issuer = self[_start.issuer]
            _start.verify_directly_issued_by(_issuer)

            _start = _issuer
        raise ValueError(f"failed to verify {cert} against chain {self.chain_prefix}")


class CAChainStore(Dict[str, CAChain]):
    """A dict that stores CA chain name and CACertChains mapping."""

    def add_chain(self, chain: CAChain) -> None:
        self[chain.chain_prefix] = chain

    def verify(self, cert: Certificate) -> CAChain | None:
        """Verify the input <cert> against this CAChainStore.

        This verification only performs the following check:
        1. ensure the cert is in valid period.
        2. check cert is signed by any one of the CA chain.

        Returns:
            Return the cachain that issues the <cert>, None if no cachain matches.
        """
        _now = datetime.now(tz=timezone.utc)
        if not (cert.not_valid_after_utc >= _now >= cert.not_valid_before_utc):
            logger.error(
                "cert is not within valid period: "
                f"{cert.not_valid_after_utc=}, {_now=}, {cert.not_valid_before_utc=}"
            )
            return

        for _, chain in self.items():
            try:
                chain.verify(cert)
                logger.info(f"verfication succeeded against: {chain.chain_prefix}")
                return chain
            except Exception as e:
                logger.info(
                    f"failed to verify against CA chain {chain.chain_prefix}: {e!r}"
                )
        logger.error(f"failed to verify {cert=} against all CA chains: {list(self)}")


def load_ca_cert_chains(cert_dir: StrOrPath) -> CAChainStore:
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

    ca_chains = CAChainStore()
    for ca_prefix in sorted(ca_set_prefix):
        try:
            ca_chain = CAChain()
            ca_chain.set_chain_prefix(ca_prefix)

            for c in cert_dir.glob(f"{ca_prefix}.*.pem"):
                _loaded_ca_cert = load_pem_x509_certificate(c.read_bytes())
                ca_chain.add_cert(_loaded_ca_cert)

            ca_chain.internal_check()
            ca_chains.add_chain(ca_chain)
        except Exception as e:
            _err_msg = f"failed to load CA chain {ca_prefix}: {e!r}"
            logger.warning(_err_msg)

    if not ca_chains:
        _err_msg = "all found CA chains are invalid, no CA chain is imported!!!"
        logger.error(_err_msg)
        raise CACertStoreInvalid(_err_msg)

    logger.info(f"loaded CA cert chains: {list(ca_chains)}")
    return ca_chains


class CAStoreMap(Dict[str, CACertStore]):
    """A dict of CACertStore with name."""

    def add_ca_store(self, name: str, store: CACertStore) -> None:
        self[name] = store

    def verify(
        self, cert: Certificate, interm_cas: list[Certificate] | None = None
    ) -> CACertStore | None:
        for _name, _store in self.items():
            _store.verify(cert, interm_cas=interm_cas or [])
            logger.info(f"verfication succeeded against CA store: {_name}")
            return _store
        logger.error(f"failed to verify {cert=} against all CA stores: {list(self)}")


def load_ca_store(cert_dir: StrOrPath) -> CAStoreMap:
    """Load root CA certs from the cert store, each root CA will be one CAStore
    for separating the environment.

    For example, if we have `dev.root.pem`, `stg.root.pem` and `prd.root.pem`, each
        for `dev`, `stg`, `prd` environment, we will get three separated CACertStore.

    Raises:
        CACertStoreInvalid on failed import.
    """
    cert_dir = Path(cert_dir)

    ca_stores = CAStoreMap()
    for _cert in cert_dir.glob("*"):
        if _cert.suffix not in [".pem", ".crt"]:
            continue  # not a cert

        try:
            _loaded_ca_cert = load_pem_x509_certificate(_cert.read_bytes())
        except Exception as e:
            logger.warning(f"{_cert} is not a cert, skip: {e}")
            continue

        if _loaded_ca_cert.issuer != _loaded_ca_cert.subject:
            continue  # not a root CA cert

        try:
            _ca_store = CACertStore()
            _ca_store.add_cert(_loaded_ca_cert)
        except Exception as e:
            logger.warning(f"{_cert} is not a valid x509 cert: {e}")
            continue
        ca_stores.add_ca_store(_cert.name, _ca_store)

    if not ca_stores:
        _err_msg = "all found CA chains are invalid, no CA chain is imported!!!"
        logger.error(_err_msg)
        raise CACertStoreInvalid(_err_msg)

    logger.info(f"loaded CA stores: {list(ca_stores)}")
    return ca_stores
