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
from functools import partial
from pathlib import Path
from typing import Dict

from OpenSSL import crypto

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


class CACertChainStore(Dict[str, crypto.X509Store]):
    """A dict-like type that stores CA chain name and CA store mapping."""


load_cert_in_pem = partial(crypto.load_certificate, crypto.FILETYPE_PEM)


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
            ca_chain = crypto.X509Store()
            for c in cert_dir.glob(f"{ca_prefix}.*.pem"):
                ca_chain.add_cert(load_cert_in_pem(c.read_bytes()))
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
