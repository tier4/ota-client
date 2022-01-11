import logging

_log_format = (
    "[%(asctime)s][%(levelname)s]-%(filename)s:%(funcName)s:%(lineno)d,%(message)s"
)
logging.basicConfig(format=_log_format)


def get_logger(name: str, level: int = logging.DEBUG) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    return logger
