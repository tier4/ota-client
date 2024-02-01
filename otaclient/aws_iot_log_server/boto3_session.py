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
import botocore.credentials
import botocore.session
import boto3
import logging
import json
import pycurl
from typing import Any, NamedTuple
from urllib.parse import urljoin

from otaclient._utils import chain_query
from otaclient.aws_iot_log_server.ggcfg import GreengrassConfig

logger = logging.getLogger(__name__)


class CredentialPack(NamedTuple):
    access_key: str
    secret_key: str
    token: str
    expiry_time: Any


class Boto3Session:
    def __init__(
        self,
        *,
        gg_config: GreengrassConfig,
        credential_provider_endpoint: str,
        role_alias: str,
    ) -> None:
        """
        Args:
            gg_config: parsed Greengrass configuration.
            credential_provider_endpoint: the HTTPS URL endpoint to get credential from.
            role_alias: path component in full credential query HTTP URL.
        """
        self._ca_cert = gg_config.ca_path
        self._cert = gg_config.certificate_path
        self._private_key = gg_config.private_key_path
        self._region = gg_config.region
        self._thing_name = gg_config.thing_name

        # NOTE: ensure endpoint URL base is ended with /
        self._credential_provider_endpoint = (
            f"{credential_provider_endpoint.rstrip('/')}/"
        )
        self._role_alias = role_alias

    def get_session(self, session_duration: int = 0):
        # ref: https://github.com/boto/botocore/blob/f1d41183e0fad31301ad7331a8962e3af6359a22/botocore/credentials.py#L368
        session_credentials = botocore.credentials.RefreshableCredentials.create_from_metadata(
            metadata=self._refresh(session_duration),
            # session is automatically refreshed with refresh_using set
            refresh_using=self._refresh,
            method="sts-assume-role",
        )
        session = botocore.session.get_session()
        session._credentials = session_credentials
        session.set_config_variable("region", self._region)
        return boto3.Session(botocore_session=session)

    def _get_body(self, url: str):
        headers = [f"x-amzn-iot-thingname:{self._thing_name}"]
        connection = pycurl.Curl()
        connection.setopt(pycurl.URL, url)

        # NOTE(20240131): TPM2.0 support, use pkcs11 interface from openssl
        _use_openssl_pkcs11 = False
        if self._private_key.startswith("pkcs11:"):
            _use_openssl_pkcs11 = True
            connection.setopt(pycurl.SSLKEYTYPE, "eng")
        if self._cert.startswith("pkcs11:"):
            _use_openssl_pkcs11 = True
            connection.setopt(pycurl.SSLCERTTYPE, "eng")
        if _use_openssl_pkcs11:
            connection.setopt(pycurl.SSLENGINE, "pkcs11")

        # server auth option
        connection.setopt(pycurl.SSL_VERIFYPEER, 1)
        connection.setopt(pycurl.CAINFO, self._ca_cert)
        connection.setopt(pycurl.CAPATH, None)
        connection.setopt(pycurl.SSL_VERIFYHOST, 2)

        # client auth option
        connection.setopt(pycurl.SSLCERT, self._cert)
        connection.setopt(pycurl.SSLKEY, self._private_key)
        connection.setopt(pycurl.HTTPHEADER, headers)

        response = connection.perform_rs()
        status = connection.getinfo(pycurl.HTTP_CODE)
        connection.close()

        if status // 100 != 2:
            raise ValueError(f"response error: {status=}")
        return response

    def _refresh(self, session_duration: int = 0) -> CredentialPack:
        # TODO: the provider endpoint should be a HTTPS URL!
        url = urljoin(
            self._credential_provider_endpoint,
            f"role-aliases/{self._role_alias}/credentials",
        )

        try:
            resp = self._get_body(url)
        except ValueError as e:
            logger.error(f"token refreshing request failed: {e!r}")
            raise

        try:
            body: dict[str, Any] = json.loads(resp)
            assert isinstance(body, dict)
        except json.JSONDecodeError as e:
            logger.error(f"resp is not valid json: {e!r}")
            raise
        except AssertionError:
            logger.error("resp should be a json object")
            raise

        expiry_time = chain_query(body, "credentials:expiration")
        logger.debug(f"session is refreshed, expiry_time: {expiry_time}")

        return CredentialPack(
            access_key=chain_query(body, "credentials:accessKeyId"),
            secret_key=chain_query(body, "credentials:secretAccessKey"),
            token=chain_query(body, "credentials:sessionToken"),
            expiry_time=expiry_time,
        )
