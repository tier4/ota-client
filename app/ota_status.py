#!/usr/bin/env python3

import tempfile
import os
import shutil


class OtaStatus:
    """
    OTA status class
    """

    def __init__(
        self,
        ota_status_file="/boot/ota/ota_status",
        ota_rollback_file="/boot/ota/ota_rollback_count",
    ):
        self.__verbose = False
        self._status_file = ota_status_file
        self._rollback_file = ota_rollback_file
        self._status = self._initial_read_ota_status()
        self._rollback_count = 0  # self._initial_read_rollback_count()

    def set_ota_status(self, ota_status):
        """
        set ota status
        """
        try:
            if self._status != ota_status:
                with tempfile.NamedTemporaryFile(delete=False) as ftmp:
                    if self.__verbose:
                        print("tmp file: " + ftmp.name)
                    with open(ftmp.name, mode="w") as f:
                        f.writelines(ota_status)
                    src = self._status_file
                    dst = self._status_file + ".old"
                    if self.__verbose:
                        print("copy src: " + src + " dst: " + dst)
                    shutil.copyfile(src, dst)
                    if self.__verbose:
                        print("backuped!")
                    shutil.move(ftmp.name, self._status_file)
                    if self.__verbose:
                        print("mopved!")
                self._status = ota_status
                os.sync()
        except:
            print("OTA status set error!")
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
        if self.__verbose:
            print("ota status file: " + self._status_file)
        try:
            if os.path.exists(self._status_file):
                with open(self._status_file, mode="r") as f:
                    status = f.readline().replace("\n", "")
                    if self.__verbose:
                        print("line: " + status)
            else:
                print("No OTA status file!:", self._status_file)
        except:
            print("OTA status read error!")
        return status

    def _initial_read_rollback_count(self):
        """"""
        count_str = "0"
        if self.__verbose:
            print("ota status file: " + self._rollback_file)
        try:
            if os.path.exists(self._rollback_file):
                with open(self._rollback_file, mode="r") as f:
                    count_str = f.readline().replace("\n", "")
                    if self.__verbose:
                        print("rollback: " + int(count_str))
            else:
                if self.__verbose:
                    print("No rollback count file!:", self._rollback_file)
                with open(self._rollback_file, mode="w") as f:
                    f.write(count_str)
        except:
            print("OTA rollback count read error!")
        if self.__verbose:
            print("count_str: ", count_str)
        return int(count_str)
