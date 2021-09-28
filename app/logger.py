import datetime
import watchtower
import logging
from boto3_session import Boto3Session

logging.basicConfig(level=logging.INFO)
_logger = logging.getLogger("otaclient")


def info(message):
    _logger.info(f"[{_current_datetime()}] {message}")


def warning(message):
    _logger.warning(f"[{_current_datetime()}] {message}")


def error(message):
    _logger.error(f"[{_current_datetime()}] {message}")


def _current_datetime():
    return datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%S')


if __name__ == "__main__":
    import time
    import os

    greengrass_config = os.environ.get("AWS_GREENGRASS_CONFIG")
    credential_provider_endpoint = os.environ.get("AWS_CREDENTIAL_PROVIDER_ENDPOINT")
    role_alias = os.environ.get("AWS_ROLE_ALIAS")
    session = Boto3Session(greengrass_config=greengrass_config,
                           credential_provider_endpoint=credential_provider_endpoint,
                           role_alias=role_alias)
    boto3_session = session.get_session(session_duration="5")
    _logger.addHandler(watchtower.CloudWatchLogHandler(
        boto3_session=boto3_session,
        log_group="/aws/greengrass/Lambda/ap-northeast-1/237086546437/fms-dev-edge-autowaret4b-edge-core",
        stream_name="2021/09/24/test2",
        create_log_group=False,
        create_log_stream=True,
    ))
    info("hello,world 1")
    error("hello,world 2")
    time.sleep(10)
    info("hello,world 3")
    error("hello,world 4")
