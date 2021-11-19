import os
import logging

import aws_gglog.logger
from configs import Config as cfg
logging.basicConfig()
logging.root.setLevel(logging.INFO)

def get_logger(name: str, level: int) -> logging.Logger:
    if os.environ.get("AWS_CLOUDWATCH_LOG_ENABLE") == "true":
        return aws_gglog.logger.Logger(name, level, cfg.LOG_FORMAT).get_logger()
    else:
        logging.basicConfig(
            format=cfg.LOG_FORMAT,
        )
        logger = logging.getLogger(name)
        logger.setLevel(level)
        return logger
