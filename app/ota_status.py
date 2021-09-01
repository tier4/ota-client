#!/usr/bin/env python3

from pathlib import Path
import tempfile
import os
import shutil

import configs
from constants import OtaStatusString
from logging import getLogger, INFO

import constants

logger = getLogger(__name__)
logger.setLevel(configs.LOG_LEVEL_TABLE.get(__name__, configs.DEFAULT_LOG_LEVEL))

default_cfg = configs.get_default_conf()


class OtaStatus:
    """
    OTA status class
    """

    # default config
    _status_file = default_cfg.OTA_STATUS_FILE
    _rollback_file = default_cfg.OTA_ROLLBACK_FILE

    def __init__(self, cfg: configs.Configuration = None):
        # manually config
        if cfg:
            self._status_file = cfg.OTA_STATUS_FILE
            self._rollback_file = cfg.OTA_ROLLBACK_FILE

        self._status = self._initial_read_ota_status(self._status_file)
        self._rollback_count = self._initial_read_rollback_count(self._rollback_file)

    def set_ota_status(self, ota_status):
        """
        set ota status
        """
        try:
            if self._status != ota_status:
                with tempfile.NamedTemporaryFile(delete=False, prefix=__name__) as ftmp:
                    logger.debug(f"tmp file: {ftmp.name}")
                    with open(ftmp.name, mode="w") as f:
                        f.writelines(ota_status)
                    src = self._status_file
                    dst = self._status_file.with_suffix(
                        self._status_file.suffix + ".old"
                    )
                    logger.debug(f"copy src: {src} dst: {dst}")
                    shutil.copyfile(src, dst)
                    logger.debug("backuped!")
                    shutil.move(ftmp.name, self._status_file)
                    logger.debug("moved!")
                self._status = ota_status
                os.sync()
        except:
            logger.exception("OTA status set error!")
            return False
        return True

    def get_ota_status(self):
        return self._status

    def inc_rollback_count(self):
        """"""
        if self._rollback_count == 0:
            self._rollback_count = 1
        with open(self._rollback_file, mode="w") as f:
            f.write(str(self._rollback_count))
        os.sync()

    def dec_rollback_count(self):
        """"""
        if self._rollback_count == 1:
            self._rollback_count = 0
        with open(self._rollback_file, mode="w") as f:
            f.write(str(self._rollback_count))
        os.sync()

    def get_rollback_count(self):
        return self._rollback_count

    def is_rollback_available(self):
        return self._rollback_count > 0

    @classmethod
    def _initial_read_ota_status(cls, _status_file):
        """
        initial read ota status
        """
        logger.debug(f"ota status file: {_status_file}")
        try:
            with open(_status_file, mode="r") as f:
                status = f.readline().replace("\n", "")
                logger.debug(f"line: {status}")
        except Exception:
            logger.warning(f"No OTA status file: {_status_file}")
            status = cls._gen_ota_status_file(_status_file)
        return status

    @classmethod
    def _initial_read_rollback_count(cls, _rollback_file):
        """"""
        count_str = "0"
        logger.debug(f"ota status file: {_rollback_file}")
        try:
            with open(_rollback_file, mode="r") as f:
                count_str = f.readline().replace("\n", "")
                logger.debug(f"rollback: {count_str}")
        except Exception:
            logger.debug(f"No rollback count file!: {_rollback_file}")
            with open(_rollback_file, mode="w") as f:
                f.write(count_str)
            os.sync()
        logger.debug(f"count_str: {count_str}")
        return int(count_str)

    @staticmethod
    def _gen_ota_status_file(ota_status_file: Path):
        """
        generate OTA status file
        """
        status = constants.OtaStatusString.NORMAL_STATE
        with tempfile.NamedTemporaryFile("w", delete=False, prefix=__name__) as f:
            tmp_file = f.name
            f.write(status)

        dir_name: Path = ota_status_file.parent
        dir_name.mkdir(exist_ok=True, parents=True)
        shutil.move(tmp_file, ota_status_file)
        logger.info(f"{ota_status_file}  generated.")

        return status
