import requests
import pycurl
import json
import yaml
import botocore.credentials
import botocore.session
import boto3
import logging
import datetime
import shlex
import subprocess
import re
from pytz import utc

from .configs import LOG_FORMAT

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
_sh = logging.StreamHandler()
fmt = logging.Formatter(fmt=LOG_FORMAT)
_sh.setFormatter(fmt)
logger.addHandler(_sh)

class Boto3Session:
    def __init__(
        self,
        config: dict,
        credential_provider_endpoint: str,
        role_alias: str,
    ):
        cfg = config

        self._ca_cert = cfg.get("ca_cert")
        self._cert = cfg.get("cert")
        self._private_key = cfg.get("private_key")
        self._region = cfg.get("region")
        self._thing_name = cfg.get("thing_name")

        self._credential_provider_endpoint = credential_provider_endpoint
        self._role_alias = role_alias

    # session is automatically refreshed
    def get_session(self, session_duration: int = 0):
        # ref: https://github.com/boto/botocore/blob/f1d41183e0fad31301ad7331a8962e3af6359a22/botocore/credentials.py#L368
        session_credentials = (
            botocore.credentials.RefreshableCredentials.create_from_metadata(
                metadata=self._refresh(session_duration),
                refresh_using=self._refresh,
                method="sts-assume-role",
            )
        )
        session = botocore.session.get_session()
        session._credentials = session_credentials
        session.set_config_variable("region", self._region)

        return boto3.Session(botocore_session=session)

    def _get_body(self, url, use_pycurl=False):
        if use_pycurl:  # pycurl implementation
            headers = [f"x-amzn-iot-thingname:{self._thing_name}"]
            connection = pycurl.Curl()
            connection.setopt(connection.URL, url)

            if self._private_key.startswith("pkcs11:"):
                connection.setopt(pycurl.SSLENGINE, "pkcs11")
                connection.setopt(pycurl.SSLKEYTYPE, "eng")

            # server auth option
            connection.setopt(connection.SSL_VERIFYPEER, True)
            connection.setopt(connection.CAINFO, self._ca_cert)
            connection.setopt(connection.CAPATH, None)
            connection.setopt(connection.SSL_VERIFYHOST, 2)

            # client auth option
            connection.setopt(connection.SSLCERT, self._cert)
            connection.setopt(connection.SSLKEY, private_key)
            connection.setopt(connection.HTTPHEADER, headers)

            response = connection.perform_rs()
            connection.close()
            return json.loads(response)
        else:  # requests implementation
            # ref: https://docs.aws.amazon.com/ja_jp/iot/latest/developerguide/authorizing-direct-aws.html
            headers = {"x-amzn-iot-thingname": self._thing_name}
            logger.info(f"url: {url}, headers: {headers}")
            try:
                response = requests.get(
                    url,
                    verify=self._ca_cert,
                    cert=(self._cert, self._private_key),
                    headers=headers,
                )
                response.raise_for_status()
                return json.loads(response.text)
            except requests.exceptions.RequestException:
                logger.warning("requests error")
                raise

    def _refresh(self, session_duration: int = 0) -> dict:
        url = f"https://{self._credential_provider_endpoint}/role-aliases/{self._role_alias}/credentials"

        try:
            body = self._get_body(url, use_pycurl=True)
        except json.JSONDecodeError:
            logger.exception(f"invalid response: resp={body}")
            raise

        expiry_time = body.get("credentials", {}).get("expiration")
        if session_duration > 0:
            now = datetime.datetime.now(tz=utc)
            new_expiry_time = now + datetime.timedelta(seconds=float(session_duration))
            expiry_time = new_expiry_time.isoformat(timespec="seconds")

        logger.info(f"session is refreshed: expiry_time: {expiry_time}")

        credentials = {
            "access_key": body.get("credentials", {}).get("accessKeyId"),
            "secret_key": body.get("credentials", {}).get("secretAccessKey"),
            "token": body.get("credentials", {}).get("sessionToken"),
            "expiry_time": expiry_time,
        }
        return credentials
