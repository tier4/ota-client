import os
import logging
from configs import config as cfg


def get_logger(name: str, level: int) -> logging.Logger:
    logging.basicConfig(format=cfg.LOG_FORMAT)
    logger = logging.getLogger(name)
    logger.setLevel(level)
    http_logging_host = os.environ.get("HTTP_LOGGING_SERVER")
    if http_logging_host:
        from ..aws_iot_log_server import CustomHttpHandler

        h = CustomHttpHandler(host="localhost:8080", url="my-ecu-id-123")
        logger.addHandler(h)

    return logger
