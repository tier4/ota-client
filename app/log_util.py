import os
import logging

import aws_gglog.logger


def get_logger(name: str, level: int) -> logging.Logger:
    if os.environ.get("AWS_CLOUDWATCH_LOG_ENABLE") == "true":
        return aws_gglog.logger.Logger(name, level).get_logger()
    else:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        return logger
