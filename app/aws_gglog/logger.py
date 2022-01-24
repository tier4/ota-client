import os
import watchtower
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from retrying import retry

sys.path.append(os.path.dirname(__file__))
from boto3_session import Boto3Session
from greengrass_config import GreengrassConfig


class _BaseLogger:
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
        if _BaseLogger._instance is None:
            _BaseLogger._instance = _BaseLogger()

        return _BaseLogger._instance

    def __init__(
        self,
        name: str = "_base_",
    ):
        if _BaseLogger._instance is not None:
            raise Exception("BaseLogger is singleton")

        self._logger = logging.getLogger(name)
        # not pass log records to root logger
        self._logger.propagate = False
        self._executor = ThreadPoolExecutor()

    def set_handlers(self, level, format: str, boto3_session_duration: int = 0):
        if self._logger.hasHandlers():
            # if handlers are already set, skip to set handlers
            return

        formatter = logging.Formatter(fmt=format)

        # set stream handler
        self._set_stream_log_handler(formatter)

        # set cloudwatch log handler
        self._set_cloudwatch_log_handler_with_executor(
            formatter, boto3_session_duration
        )

        self._logger.setLevel(level)
        self._logger.info("handlers are set to base logger")

    def _set_stream_log_handler(self, formatter: logging.Formatter):
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        self._logger.addHandler(handler)

    @retry(wait_exponential_multiplier=1_000, wait_exponential_max=4_000)
    def _set_cloudwatch_log_handler(
        self,
        formatter: logging.Formatter,
        boto3_session_duration: int,
        event: Event,
    ):
        try:
            config = _BaseLogger._get_config_from_environment()
            greengrass_config = config.get("AWS_GREENGRASS_CONFIG")
            if greengrass_config:
                session_config = GreengrassConfig.parse_config(greengrass_config)
            else:
                session_config = {
                    "ca_cert": config.get("AWS_CA_CERT_FILE"),
                    "private_key": config.get("AWS_PRIVATE_KEY_FILE"),
                    "cert": config.get("AWS_CERT_FILE"),
                    "region": config.get("AWS_REGION"),
                    "thing_name": config.get("AWS_THING_NAME"),
                }
            session = Boto3Session(
                config=session_config,
                credential_provider_endpoint=config["AWS_CREDENTIAL_PROVIDER_ENDPOINT"],
                role_alias=config["AWS_ROLE_ALIAS"],
            )
            boto3_session = session.get_session(session_duration=boto3_session_duration)
            stream_name = self._get_stream_name(session._thing_name)
            handler = watchtower.CloudWatchLogHandler(
                boto3_session=boto3_session,
                log_group=config["AWS_CLOUDWATCH_LOG_GROUP"],
                stream_name=stream_name,
                create_log_group=True,
                create_log_stream=True,
                send_interval=4,
            )
            handler.setFormatter(formatter)
            self._logger.addHandler(handler)
        except Exception:
            self.logger.exception("failed to setup cloudwatch log handler")
            self._logger.warning("failed to setup cloudwatch log handler")
            raise
        finally:
            event.set()

    def _set_cloudwatch_log_handler_with_executor(
        self, formatter: logging.Formatter, boto3_session_duration: int
    ):
        event = Event()
        self._executor.submit(
            self._set_cloudwatch_log_handler, formatter, boto3_session_duration, event
        )
        event.wait()  # this makes sure _set_cloudwatch_log_handler has been started.

    @staticmethod
    def _get_config_from_environment() -> dict:
        config = {}

        keys_req = (
            "AWS_CREDENTIAL_PROVIDER_ENDPOINT",
            "AWS_ROLE_ALIAS",
            "AWS_CLOUDWATCH_LOG_GROUP",
        )
        for key in keys_req:
            config[key] = os.environ.get(key)
            if not config[key]:
                raise Exception(f"environment variable {key} is not set")

        keys_opt = (
            "AWS_GREENGRASS_CONFIG",
            "AWS_CA_CERT_FILE",
            "AWS_PRIVATE_KEY_FILE",
            "AWS_CERT_FILE",
            "AWS_REGION",
            "AWS_THING_NAME",
        )
        for key in keys_opt:
            value = os.environ.get(key)
            if value:
                config[key] = value

        return config

    @staticmethod
    def _get_stream_name(thing_name: str):
        fmt = "{strftime:%Y/%m/%d}"
        return f"{fmt}/{thing_name}"

    def get_logger(self) -> logging.Logger:
        return self._logger


class Logger:
    def __init__(
        self,
        name: str,
        level=logging.INFO,
        format="[%(asctime)s][%(levelname)s]-%(filename)s:%(lineno)d,%(message)s",
    ):
        _BaseLogger.get_instance().set_handlers(level, format)
        base_logger = _BaseLogger.get_instance().get_logger()
        self._logger = logging.getLogger(f"{base_logger.name}.{name}")
        self._logger.setLevel(level)

    def get_logger(self) -> logging.Logger:
        return self._logger


if __name__ == "__main__":
    """
    examples
    """

    format = "### %(asctime)s [%(levelname)s]-%(filename)s:%(lineno)d,%(message)s"
    log1 = Logger("foo", level=logging.INFO, format=format).get_logger()
    log1.debug("hello,world")
    log1.info("hello,world")
    log1.warning("hello,world")
    log1.error("hello,world")

    log2 = Logger("bar", level=logging.DEBUG).get_logger()
    log2.debug("hello,world")
    log2.info("hello,world")
    log2.warning("hello,world")
    log2.error("hello,world")
