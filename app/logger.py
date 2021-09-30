import datetime
import os
import watchtower
import logging
import json
from boto3_session import Boto3Session

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
_sh = logging.StreamHandler()
logger.addHandler(_sh)


class BaseLogger:
    """
    Create a logger to log in CloudWatchLog and stream.

    Following environment variables should be set.
      AWS_GREENGRASS_CONFIG: path to greengrass config file
      AWS_CREDENTIAL_PROVIDER_ENDPOINT: credentials provider endpoint for your AWS account
      AWS_ROLE_ALIAS: role alias name
      AWS_CLOUDWATCH_LOG_GROUP: log group name in CloudWatchLog
    """
    _instance = None

    @staticmethod
    def get_instance():
        if BaseLogger._instance is None:
            BaseLogger()
        else:
            return BaseLogger._instance

    def __init__(self, name: str = "_base_", level: str = logging.INFO, boto3_session_duration: int = 0):
        if BaseLogger._instance is not None:
            raise Exception("BaseLogger is singleton")

        self._logger = logging.getLogger(name)
        self._logger.setLevel(level)

        # set stream handler
        self._set_stream_log_handler()

        # set cloudwatch log handler
        self._set_cloudwatch_log_handler(boto3_session_duration)

        BaseLogger._instance = self
        logger.info("base logger is created")

    def _set_stream_log_handler(self):
        # log is formatted as follows:
        # [2021-09-29 17:42:50,607][INFO]-logger.py:108,hello,world
        formatter = logging.Formatter(
            fmt="[%(asctime)s][%(levelname)s]-%(filename)s:%(lineno)d,%(message)s",
        )

        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        self._logger.addHandler(handler)

    def _set_cloudwatch_log_handler(self, boto3_session_duration: int):
        try:
            config = BaseLogger._get_config()
            session = Boto3Session(greengrass_config=config["AWS_GREENGRASS_CONFIG"],
                                   credential_provider_endpoint=config["AWS_CREDENTIAL_PROVIDER_ENDPOINT"],
                                   role_alias=config["AWS_ROLE_ALIAS"])
            boto3_session = session.get_session(session_duration=boto3_session_duration)
            stream_name = self._get_stream_name(config["AWS_GREENGRASS_CONFIG"])
            handler = watchtower.CloudWatchLogHandler(
                boto3_session=boto3_session,
                log_group=config["AWS_CLOUDWATCH_LOG_GROUP"],
                stream_name=stream_name,
                create_log_group=True,
                create_log_stream=True,
            )
            json_format = json.dumps({
                "time": "%(asctime)s",
                "level": "%(levelname)s",
                "filename": "%(filename)s",
                "lineno": "%(lineno)d",
                "message": "%(message)s",
            })
            # log is formatted as follows:
            # {"time": "2021-09-30 10:08:58,133", "level": "WARNING", "filename": "logger.py", "lineno": "128", "message": "hello,world"}
            formatter = logging.Formatter(fmt=json_format)
            handler.setFormatter(formatter)
            self._logger.addHandler(handler)
        except:
            logger.exception("failed to setup cloudwatch log handler")

    @staticmethod
    def _get_config() -> dict:
        keys = ("AWS_GREENGRASS_CONFIG",
                "AWS_CREDENTIAL_PROVIDER_ENDPOINT",
                "AWS_ROLE_ALIAS",
                "AWS_CLOUDWATCH_LOG_GROUP",
                )
        config = {}
        for key in keys:
            config[key] = os.environ.get(key)
            if not config[key]:
                raise Exception(f"environment variable {key} is not set")

        return config

    @staticmethod
    def _get_stream_name(boto3_config: str):
        config = Boto3Session.parse_config(boto3_config)
        thing_name = config.get("thing_name", "unknown")
        fmt = "{strftime:%Y/%m/%d}"
        return f"{fmt}/{thing_name}"

    def get_logger(self) -> logging.Logger:
        return self._logger


class Logger:
    def __init__(self, name: str, level=logging.INFO):
        base_logger = BaseLogger.get_instance().get_logger()
        self._logger = logging.getLogger(f"{base_logger.name}.{name}")
        self._logger.setLevel(level)

    def get_logger(self) -> logging.Logger:
        return self._logger


if __name__ == "__main__":
    """
    examples
    """
    BaseLogger("example.service")

    log1 = Logger("foo").get_logger()
    log1.debug("hello,world")
    log1.info("hello,world")
    log1.warning("hello,world")
    log1.error("hello,world")

    log2 = Logger("bar", logging.DEBUG).get_logger()
    log2.debug("hello,world")
    log2.info("hello,world")
    log2.warning("hello,world")
    log2.error("hello,world")
