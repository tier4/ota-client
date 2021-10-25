import os
import logging
import configs as cfg

import aws_gglog.logger


def get_logger(name: str, level: int, fmt: str) -> logging.Logger:
    if os.environ.get("AWS_CLOUDWATCH_LOG_ENABLE") == "true":
        return aws_gglog.logger.Logger(name, level, cfg.LOG_FORMAT).get_logger()
    else:
        logging.basicConfig(format=cfg.LOG_FORMAT)
        logger = logging.getLogger(name)
        logger.setLevel(level)
        return logger
