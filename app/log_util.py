import os
import logging

import aws_gglog.logger
import configs


def get_logger(name: str, level: int) -> logging.Logger:
    if os.environ.get("AWS_CLOUDWATCH_LOG_ENABLE") == "true":
        return aws_gglog.logger.Logger(name, level, configs.LOG_FORMAT).get_logger()
    else:
        logging.basicConfig(
            format=configs.LOG_FORMAT,
        )
        logger = logging.getLogger(name)
        logger.setLevel(level)
        return logger
