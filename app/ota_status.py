#!/usr/bin/env python3

import tempfile
import os
import shutil

from logging import getLogger, INFO

logger = getLogger(__name__)
logger.setLevel(INFO)


class OtaStatus:
    """
    OTA status class
    """

    def __init__(
        self,
        ota_status_file="/boot/ota/ota_status",
        ota_rollback_file="/boot/ota/ota_rollback_count",
    ):
        self._status_file = ota_status_file
        self._rollback_file = ota_rollback_file
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
                    dst = self._status_file + ".old"
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

    def _initial_read_ota_status(self):
        """
        initial read ota status
        """
        status = ""
        logger.debug(f"ota status file: {self._status_file}")
        if os.path.exists(self._status_file):
            with open(self._status_file, mode="r") as f:
                status = f.readline().replace("\n", "")
                logger.debug(f"line: {status}")
        else:
            logger.warning(f"No OTA status file: {self._status_file}")
            self._gen_ota_status_file(self._status_file)
        return status

    def _initial_read_rollback_count(self):
        """"""
        count_str = "0"
        logger.debug(f"ota status file: {self._rollback_file}")
        if os.path.exists(self._rollback_file):
            with open(self._rollback_file, mode="r") as f:
                count_str = f.readline().replace("\n", "")
                logger.debug(f"rollback: {count_str}")
        else:
            logger.debug(f"No rollback count file!: {self._rollback_file}")
            with open(self._rollback_file, mode="w") as f:
                f.write(count_str)
        logger.debug(f"count_str: {count_str}")
        return int(count_str)

    @staticmethod
    def _gen_ota_status_file(ota_status_file):
        """
        generate OTA status file
        """
        with tempfile.NamedTemporaryFile(delete=False) as ftmp:
            tmp_file = ftmp.name
            with open(ftmp.name, "w") as f:
                f.write("NORMAL")
                f.flush()
        os.sync()
        dir_name = os.path.dirname(ota_status_file)
        if not os.path.exists(dir_name):
            os.makedirs(dir_name)
        shutil.move(tmp_file, ota_status_file)
        logger.info(f"{ota_status_file}  generated.")
        os.sync()
        return True
