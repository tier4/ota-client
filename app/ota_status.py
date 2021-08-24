#!/usr/bin/env python3

import tempfile
import os
import shutil

import configs as cfg
from constants import OtaStatusString
from logging import getLogger, INFO

import constants

logger = getLogger(__name__)
logger.setLevel(INFO)


class OtaStatus:
    """
    OTA status class
    """

    _status_file = cfg.OTA_STATUS_FILE
    _rollback_file = cfg.OTA_ROLLBACK_FILE

    def __init__(self):
        self._status = self._initial_read_ota_status()
        self._rollback_count = self._initial_read_rollback_count()

    def set_ota_status(self, ota_status):
        """
        set ota status
        """
        try:
            if self._status != ota_status:
                with tempfile.NamedTemporaryFile(delete=False) as ftmp:
                    logger.debug(f"tmp file: {ftmp.name}")
                    with open(ftmp.name, mode="w") as f:
                        f.writelines(ota_status)
                    src = self._status_file
                    dst = self._status_file.with_suffix(".old")
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
    def _initial_read_ota_status(cls):
        """
        initial read ota status
        """
        logger.debug(f"ota status file: {cls._status_file}")
        try:
            with open(cls._status_file, mode="r") as f:
                status = f.readline().replace("\n", "")
                logger.debug(f"line: {status}")
        except Exception:
            logger.warning(f"No OTA status file: {cls._status_file}")
            status = cls._gen_ota_status_file(cls._status_file)
        return status

    @classmethod
    def _initial_read_rollback_count(cls):
        """"""
        count_str = "0"
        logger.debug(f"ota status file: {cls._rollback_file}")
        try:
            with open(cls._rollback_file, mode="r") as f:
                count_str = f.readline().replace("\n", "")
                logger.debug(f"rollback: {count_str}")
        except Exception:
            logger.debug(f"No rollback count file!: {cls._rollback_file}")
            with open(cls._rollback_file, mode="w") as f:
                f.write(count_str)
            os.sync()
        logger.debug(f"count_str: {count_str}")
        return int(count_str)

    @staticmethod
    def _gen_ota_status_file(ota_status_file):
        """
        generate OTA status file
        """
        status = constants.OtaStatusString.NORMAL_STATE
        with tempfile.NamedTemporaryFile(delete=False) as ftmp:
            tmp_file = ftmp.name
            with open(ftmp.name, "w") as f:
                f.write(status)
                f.flush()
        os.sync()
        dir_name = os.path.dirname(ota_status_file)
        os.makedirs(dir_name, exist_ok=True)
        shutil.move(tmp_file, ota_status_file)
        logger.info(f"{ota_status_file}  generated.")
        os.sync()
        return status
