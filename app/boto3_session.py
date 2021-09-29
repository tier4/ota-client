import requests
import json
import botocore.credentials
import botocore.session
import boto3
import re
import logging
import datetime
from pytz import utc

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class Boto3Session:
    def __init__(self, greengrass_config: str, credential_provider_endpoint: str, role_alias: str):
        cfg = Boto3Session.parse_config(greengrass_config)

        self._ca_cert = cfg.get("ca_cert")
        self._cert = cfg.get("cert")
        self._private_key = cfg.get("private_key")
        self._region = cfg.get("region")
        self._thing_name = cfg.get("thing_name")

        self._credential_provider_endpoint = credential_provider_endpoint
        self._role_alias = role_alias

    @staticmethod
    def parse_config(config) -> dict:
        try:
            with open(config) as f:
                cfg = json.load(f)
        except FileNotFoundError:
            logger.exception(f"config file is not found: file={config}")
            raise
        except json.JSONDecodeError as e:
            logger.exception(f"invalid json format: {e}")
            raise

        ca_path = cfg.get("crypto", {}).get("caPath")
        private_key_path = cfg.get("crypto", {}).get("principals", {}).get("IoTCertificate", {}).get("privateKeyPath")
        certificate_path = cfg.get("crypto", {}).get("principals", {}).get("IoTCertificate", {}).get("certificatePath")
        thing_arn = cfg.get("coreThing", {}).get("thingArn")

        strs = thing_arn.split(":", 6)
        if len(strs) != 6:
            logger.error(f"invalid thing arn: thing_arn={thing_arn}")
            return {}

        region = strs[3]
        thing_name = strs[5]

        def remove_prefix(s, prefix):
            return re.sub(f"^{prefix}", "", s)

        return {
            "ca_cert": remove_prefix(ca_path, "file://"),
            "private_key": remove_prefix(private_key_path, "file://"),
            "cert": remove_prefix(certificate_path, "file://"),
            "region": region,
            "thing_name": remove_prefix(thing_name, "thing/"),
        }

    # session is automatically refreshed
    def get_session(self, session_duration: "sec" = ""):
        # ref: https://github.com/boto/botocore/blob/f1d41183e0fad31301ad7331a8962e3af6359a22/botocore/credentials.py#L368
        session_credentials = botocore.credentials.RefreshableCredentials.create_from_metadata(
            metadata=self._refresh(session_duration),
            refresh_using=self._refresh,
            method="sts-assume-role",
        )
        session = botocore.session.get_session()
        session._credentials = session_credentials
        session.set_config_variable("region", self._region)

        return boto3.Session(botocore_session=session)

    def _refresh(self, session_duration: "sec" = "") -> dict:
        # ref: https://docs.aws.amazon.com/ja_jp/iot/latest/developerguide/authorizing-direct-aws.html
        url = f"https://{self._credential_provider_endpoint}/role-aliases/{self._role_alias}/credentials"
        headers = {"x-amzn-iot-thingname": self._thing_name}
        logger.info(f"url: {url}, headers: {headers}")
        try:
            response = requests.get(
                url, verify=self._ca_cert, cert=(self._cert, self._private_key), headers=headers
            )
            response.raise_for_status()
        except requests.exceptions.RequestException:
            logger.exception("requests error")
            raise

        try:
            body = json.loads(response.text)
        except json.JSONDecodeError:
            logger.exception(f"invalid response: resp={response.text}")
            raise

        expiry_time = body.get("credentials", {}).get("expiration")
        if session_duration != "":
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
