import datetime
import watchtower
import logging
from boto3_session import Boto3Session

logging.basicConfig(level=logging.INFO)
_logger = logging.getLogger('sample_logger')


def info(message):
    _logger.error('[%s] %s' % (current_datetime(), message))


def error(message):
    _logger.error(f'[{current_datetime()}] {message}')


def current_datetime():
    return datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%S')


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="greengrass config.json", required=True)
    args = parser.parse_args()
    boto3_session = Boto3Session(args.config,
                                 credential_provider_endpoint="cfm5qkounell8.credentials.iot.ap-northeast-1.amazonaws.com",
                                 role_alias="fms-dev-autoware-adapter-credentials-iot-secrets-access-role-alias")
    _logger.addHandler(watchtower.CloudWatchLogHandler(
        boto3_session=boto3_session.get_session(),
        log_group="/aws/greengrass/Lambda/ap-northeast-1/237086546437/fms-dev-edge-autowaret4b-edge-core",
        stream_name="2021/09/24/test2",
        create_log_group=False,
        create_log_stream=True,
    ))
    _logger.info("hello,world 1")
    _logger.error("hello,world 2")
