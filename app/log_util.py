import os
import sys
import yaml
import logging
from pathlib import Path
from configs import config as cfg

_logger = None


def get_logger(name: str, level: int) -> logging.Logger:
    global _logger
    if _logger:
        return _logger
    _logger = _get_logger(name, level)
    return _logger


# NOTE: EcuInfo imports this log_util so independent get_ecu_id are required.
def _get_ecu_id():
    try:
        with open(cfg.ECU_INFO_FILE) as f:
            ecu_info = yaml.load(f, Loader=yaml.SafeLoader)
            return ecu_info["ecu_id"]
    except Exception:
        return "autoware"


def _get_logger(name: str, level: int) -> logging.Logger:
    logging.basicConfig(format=cfg.LOG_FORMAT)
    logger = logging.getLogger(name)
    logger.setLevel(level)
    http_logging_host = os.environ.get("HTTP_LOGGING_SERVER")
    if http_logging_host:
        sys.path.append(
            str(Path(__file__).parent.parent / "aws-iot-log-server" / "app")
        )
        from custom_http_handler import CustomHttpHandler

        h = CustomHttpHandler(host=http_logging_host, url=_get_ecu_id())
        fmt = logging.Formatter(fmt=cfg.LOG_FORMAT)
        h.setFormatter(fmt)
        logger.addHandler(h)

    return logger
