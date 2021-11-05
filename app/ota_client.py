import tempfile
import requests
import shutil
import urllib.parse
import re
import os
import time
import json
import operator
from hashlib import sha256
from pathlib import Path
from json.decoder import JSONDecodeError
from multiprocessing import Pool, Manager
from threading import Lock
from functools import partial, reduce
from collections import Counter
from enum import Enum, unique
from requests.exceptions import RequestException

from ota_status import OtaStatusControl, OtaStatus
from ota_metadata import OtaMetadata
from ota_error import OtaErrorUnrecoverable, OtaErrorRecoverable
from copy_tree import CopyTree
import configs as cfg
import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


def file_sha256(filename: Path) -> str:
    with open(filename, "rb") as f:
        return sha256(f.read()).hexdigest()


def verify_file(filename: Path, filehash: str) -> bool:
    return file_sha256(filename) == filehash


class _BaseInf:
    _base_pattern = re.compile(
        r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+),(?P<left_over>.*)"
    )

    @staticmethod
    def de_escape(s: str) -> str:
        return s.replace(r"'\''", r"'")

    def __init__(self, info: str):
        match_res: re.Match = self._base_pattern.match(info.strip("\n"))
        assert match_res is not None
        self.mode = int(match_res.group("mode"), 8)
        self.uid = int(match_res.group("uid"))
        self.gid = int(match_res.group("gid"))

        self._left: str = match_res.group("left_over")


class DirectoryInf(_BaseInf):
    """
    Directory file information class
    """

    def __init__(self, info):
        super().__init__(info)
        self.path = Path(self.de_escape(self._left[1:-1]))


class SymbolicLinkInf(_BaseInf):
    """
    Symbolik link information class
    """

    _pattern = re.compile(r"'(?P<link>.+)((?<!\')',')(?P<target>.+)'")

    def __init__(self, info):
        super().__init__(info)
        res = self._pattern.match(self._left)
        assert res is not None
        self.slink = Path(self.de_escape(res.group("link")))
        self.srcpath = Path(self.de_escape(res.group("target")))


class RegularInf(_BaseInf):
    """
    Regular file information class
    """

    _pattern = re.compile(r"(?P<nlink>\d+),(?P<hash>\w+),'(?P<path>.+)'")

    def __init__(self, info):
        super().__init__(info)

        res = self._pattern.match(self._left)
        assert res is not None
        self.nlink = int(res.group("nlink"))
        self.sha256hash = res.group("hash")
        self.path = Path(self.de_escape(res.group("path")))


class PersistentInf(_BaseInf):
    """
    Persistent file information class
    """

    def __init__(self, info: str):
        self.path = Path(self.de_escape(info[1:-1]))


@unique
class OtaClientFailureType(Enum):
    NO_FAILURE = 0
    RECOVERABLE = 1
    UNRECOVERABLE = 2


@unique
class OtaClientUpdatePhase(Enum):
    INITIAL = 0
    METADATA = 1
    DIRECTORY = 2
    SYMLINK = 3
    REGULAR = 4
    PERSISTENT = 5
    POST_PROCESSING = 6


class OtaClientStatistics(object):
    def __init__(self):
        self.clear()

    def clear(self):
        self.total_files = 0
        self.files_processed = 0

        self.files_processed_copy = 0
        self.files_processed_link = 0
        self.files_processed_download = 0

        self.file_size_processed_copy = 0
        self.file_size_processed_link = 0
        self.file_size_processed_download = 0

        self.elapsed_time_copy = 0
        self.elapsed_time_link = 0
        self.elapsed_time_download = 0

        self.errors_download = 0


class OtaClient:
    MOUNT_POINT = cfg.MOUNT_POINT  # Path("/mnt/standby")
    PASSWD_FILE = cfg.PASSWD_FILE  # Path("/etc/passwd")
    GROUP_FILE = cfg.GROUP_FILE  # Path("/etc/group")

    def __init__(self):
        self._ota_status = OtaStatusControl()
        self._mount_point = OtaClient.MOUNT_POINT
        self._passwd_file = OtaClient.PASSWD_FILE
        self._group_file = OtaClient.GROUP_FILE

        self._lock = Lock()  # NOTE: can't be referenced from pool.apply_async target.
        self._failure_type = OtaClientFailureType.NO_FAILURE
        self._failure_reason = ""
        self._update_phase = OtaClientUpdatePhase.INITIAL

        # statistics
        self._statistics = OtaClientStatistics()

    def update(self, version, url_base, cookies_json: str):
        # check if there is an on-going update
        try:
            self._ota_status.check_update_status()
        except OtaErrorRecoverable:
            # not setting ota_status
            logger.exception("check_update_status")
            return OtaClientFailureType.RECOVERABLE

        try:
            cookies = json.loads(cookies_json)
            self._update(version, url_base, cookies)
            return self._result_ok()
        except (JSONDecodeError, OtaErrorRecoverable) as e:
            logger.exception("recoverable")
            self._ota_status.set_ota_status(OtaStatus.FAILURE)
            return self._result_recoverable(e)
        except (OtaErrorUnrecoverable, Exception) as e:
            logger.exception("unrecoverable")
            self._ota_status.set_ota_status(OtaStatus.FAILURE)
            return self._result_unrecoverable(e)

    def rollback(self):
        # check if there is an on-going rollback
        try:
            self._ota_status.check_rollback_status()
        except OtaErrorRecoverable:
            # not setting ota_status
            logger.exception("check_rollback_status")
            return OtaClientFailureType.RECOVERABLE

        try:
            self._rollback()
            return self._result_ok()
        except OtaErrorRecoverable as e:
            logger.exception("recoverable")
            self._ota_status.set_ota_status(OtaStatus.ROLLBACK_FAILURE)
            return self._result_recoverable(e)
        except (OtaErrorUnrecoverable, Exception) as e:
            logger.exception("unrecoverable")
            self._ota_status.set_ota_status(OtaStatus.ROLLBACK_FAILURE)
            return self._result_unrecoverable(e)

    # NOTE: status should not update any internal status
    def status(self):
        try:
            status = self._status()
            return OtaClientFailureType.NO_FAILURE, status
        except OtaErrorRecoverable:
            logger.exception("recoverable")
            return OtaClientFailureType.RECOVERABLE, None
        except (OtaErrorUnrecoverable, Exception):
            logger.exception("unrecoverable")
            return OtaClientFailureType.UNRECOVERABLE, None

    """ private functions from here """

    def _result_ok(self):
        self._failure_type = OtaClientFailureType.NO_FAILURE
        self._failure_reason = ""
        return OtaClientFailureType.NO_FAILURE

    def _result_recoverable(self, e):
        logger.exception(e)
        self._failure_type = OtaClientFailureType.RECOVERABLE
        self._failure_reason = str(e)
        return OtaClientFailureType.RECOVERABLE

    def _result_unrecoverable(self, e):
        logger.exception(e)
        self._failure_type = OtaClientFailureType.UNRECOVERABLE
        self._failure_reason = str(e)
        return OtaClientFailureType.UNRECOVERABLE

    def _update(self, version, url_base, cookies):
        logger.info(f"{version=},{url_base=},{cookies=}")
        """
        e.g.
        cookies = {
            "CloudFront-Policy": "eyJTdGF0ZW1lbnQ...",
            "CloudFront-Signature": "o4ojzMrJwtSIg~izsy...",
            "CloudFront-Key-Pair-Id": "K2...",
        }
        """
        self._update_phase = OtaClientUpdatePhase.INITIAL
        self._statistics.clear()

        logger.info(f"version={version}, url_base={url_base}, cookies={cookies}")
        # enter update
        self._ota_status.enter_updating(version, self._mount_point)

        # process metadata.jwt
        self._update_phase = OtaClientUpdatePhase.METADATA
        url = f"{url_base}/"
        metadata = self._process_metadata(url, cookies)

        # process directory file
        self._update_phase = OtaClientUpdatePhase.DIRECTORY
        self._process_directory(
            url, cookies, metadata.get_directories_info(), self._mount_point
        )

        # process symlink file
        self._update_phase = OtaClientUpdatePhase.SYMLINK
        self._process_symlink(
            url, cookies, metadata.get_symboliclinks_info(), self._mount_point
        )

        # process regular file
        self._update_phase = OtaClientUpdatePhase.REGULAR
        self._process_regular(
            url,
            cookies,
            metadata.get_regulars_info(),
            metadata.get_rootfsdir_info()["file"],
            self._mount_point,
        )

        # process persistent file
        self._update_phase = OtaClientUpdatePhase.PERSISTENT
        self._process_persistent(
            url, cookies, metadata.get_persistent_info(), self._mount_point
        )

        # leave update
        self._update_phase = OtaClientUpdatePhase.POST_PROCESSING
        self._ota_status.leave_updating(self._mount_point)

    def _rollback(self):
        # enter rollback
        self._ota_status.enter_rollbacking()
        # leave rollback
        self._ota_status.leave_rollbacking()

    def _status(self):
        return {
            "status": self._ota_status.get_ota_status().name,
            "failure_type": self._failure_type.name,
            "failure_reason": self._failure_reason,
            "version": self._ota_status.get_version(),
            "update_progress": {
                "phase": self._update_phase.name,
                "total_regular_files": self._statistics.total_files,
                "regular_files_processed": self._statistics.files_processed,
                "files_processed_copy": self._statistics.files_processed_copy,
                "files_processed_link": self._statistics.files_processed_link,
                "files_processed_download": self._statistics.files_processed_download,
                "file_size_processed_copy": self._statistics.file_size_processed_copy,
                "file_size_processed_link": self._statistics.file_size_processed_link,
                "file_size_processed_download": self._statistics.file_size_processed_download,
                "elapsed_time_copy": self._statistics.elapsed_time_copy,
                "elapsed_time_link": self._statistics.elapsed_time_link,
                "elapsed_time_download": self._statistics.elapsed_time_download,
                "errors_download": self._statistics.errors_download,
            },
        }

    @staticmethod
    def _download(url, cookies, dst, digest, retry=5):
        def _requests_get():
            headers = {}
            headers["Accept-encording"] = "gzip"
            response = requests.get(url, headers=headers, cookies=cookies, timeout=10)
            # For 50x, especially 503, wait a few seconds and try again.
            if response.status_code // 100 == 5:
                time.sleep(10)
            response.raise_for_status()
            return response

        count = 0
        while True:
            last_error = ""
            try:
                response = _requests_get()
                break
            except (
                requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError,
            ) as e:
                count += 1
                last_error = (
                    f"requests timeout or connection error: {e},{url=} ({count})"
                )
                logger.warning(last_error)
            except RequestException as e:
                count += 1
                status_code = e.response.status_code
                last_error = f"requests error: {status_code=},{url=} ({count})"
                logger.warning(last_error)
            except Exception as e:
                count += 1
                last_error = f"requests error unknown: {e},{url=}, ({count})"
                logger.warning(last_error)
            finally:
                if count == retry:
                    logger.error(last_error)
                    raise OtaErrorRecoverable(last_error)

        with open(dst, "wb") as f:
            m = sha256()
            total_length = response.headers.get("content-length")
            if total_length is None:
                m.update(response.content)
                f.write(response.content)
            else:
                dl = 0
                for data in response.iter_content(chunk_size=4096):
                    dl += len(data)
                    m.update(data)
                    f.write(data)

        calc_digest = m.hexdigest()
        if digest and digest != calc_digest:
            raise OtaErrorRecoverable(
                f"hash error: act={calc_digest}, exp={digest}, {url=}"
            )
        return count

    def _verify_metadata(self, url_base, cookies, list_info, metadata):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            url = urllib.parse.urljoin(url_base, list_info["file"])
            OtaClient._download(url, cookies, file_name, list_info["hash"])
            metadata.verify(open(file_name).read())
            logger.info("done")

    def _process_metadata(self, url_base, cookies):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / "metadata.jwt"
            url = urllib.parse.urljoin(url_base, "metadata.jwt")
            OtaClient._download(url, cookies, file_name, None)
            metadata = OtaMetadata(open(file_name, "r").read())
            certificate_info = metadata.get_certificate_info()
            self._verify_metadata(url_base, cookies, certificate_info, metadata)
            logger.info("done")
            return metadata

    def _process_directory(self, url_base, cookies, list_info, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            url = urllib.parse.urljoin(url_base, list_info["file"])
            OtaClient._download(url, cookies, file_name, list_info["hash"])
            self._create_directories(file_name, standby_path)
            logger.info("done")

    def _process_symlink(self, url_base, cookies, list_info, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            url = urllib.parse.urljoin(url_base, list_info["file"])
            OtaClient._download(url, cookies, file_name, list_info["hash"])
            self._create_symbolic_links(file_name, standby_path)
            logger.info("done")

    def _process_regular(self, url_base, cookies, list_info, rootfsdir, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            url = urllib.parse.urljoin(url_base, list_info["file"])
            OtaClient._download(url, cookies, file_name, list_info["hash"])
            url_rootfsdir = urllib.parse.urljoin(url_base, f"{rootfsdir}/")
            self._create_regular_files(url_rootfsdir, cookies, file_name, standby_path)
            logger.info("done")

    def _process_persistent(self, url_base, cookies, list_info, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            url = urllib.parse.urljoin(url_base, list_info["file"])
            OtaClient._download(url, cookies, file_name, list_info["hash"])
            self._copy_persistent_files(file_name, standby_path)
            logger.info("done")

    def _create_directories(self, list_file, standby_path):
        lines = open(list_file).read().splitlines()
        for line in lines:
            dirinf = DirectoryInf(line)
            target_path = standby_path.joinpath(dirinf.path.relative_to("/"))
            target_path.mkdir(mode=dirinf.mode, parents=True, exist_ok=True)
            os.chown(target_path, dirinf.uid, dirinf.gid)
            os.chmod(target_path, dirinf.mode)

    def _create_symbolic_links(self, list_file, standby_path):
        lines = open(list_file).read().splitlines()
        for line in lines:
            # NOTE: symbolic link in /boot directory is not supported. We don't use it.
            slinkf = SymbolicLinkInf(line)
            slink = standby_path.joinpath(slinkf.slink.relative_to("/"))
            slink.symlink_to(slinkf.srcpath)
            os.chown(slink, slinkf.uid, slinkf.gid, follow_symlinks=False)

    def _set_statistics(self, processed_list):
        st = self._statistics
        st.files_processed = len(processed_list)

        def set_st(op, files_processed, file_size_processed, elapsed_time):
            _list = []
            for i in processed_list[: st.files_processed]:
                if i["op"] == op:
                    i.pop("op")  # remove 'op' element
                    _list.append(i)
            setattr(st, files_processed, len(_list))

            _list = _list if _list else [{}]  # add {} if empty
            _total = reduce(operator.add, map(Counter, _list))
            setattr(st, file_size_processed, _total.get("size", 0))
            setattr(st, elapsed_time, _total.get("elapsed", 0))
            if op == "download":
                st.errors_download = _total.get("errors", 0)

        set_st(
            "copy",
            "files_processed_copy",
            "file_size_processed_copy",
            "elapsed_time_copy",
        )
        set_st(
            "link",
            "files_processed_link",
            "file_size_processed_link",
            "elapsed_time_link",
        )
        set_st(
            "download",
            "files_processed_download",
            "file_size_processed_download",
            "elapsed_time_download",
        )

    def _create_regular_files(self, url_base: str, cookies, list_file, standby_path):
        reginf_list_raw_lines = open(list_file).read().splitlines()
        self._statistics.total_files = len(reginf_list_raw_lines)

        with Manager() as manager:
            error_queue = manager.Queue()
            # NOTE: manager.Value doesn't work properly.
            """
            processed_list have dictionaries as follows:
            {"size": int}  # file size
            {"elapsed": int}  # elapsed time in seconds
            {"op": str}  # operation. "copy", "link" or "download"
            {"errors": int}  # number of errors that occurred when downloading.
            """
            processed_list = manager.list()

            def error_callback(e):
                error_queue.put(e)

            boot_standby_path = self._ota_status.get_standby_boot_partition_path()
            # bind the required options before we use this method
            _create_regfile_func = partial(
                self._create_regular_file,
                url_base=url_base,
                cookies=cookies,
                standby_path=standby_path,
                processed_list=processed_list,
                boot_standby_path=boot_standby_path,
            )

            with Pool() as pool:
                hardlink_dict = dict()  # sha256hash[tuple[reginf, event]]

                # imap_unordered return a lazy iterator without blocking
                reginf_list = pool.imap_unordered(RegularInf, reginf_list_raw_lines)
                for reginf in reginf_list:
                    if reginf.nlink >= 2:
                        prev_reginf, event = hardlink_dict.setdefault(
                            reginf.sha256hash, (reginf, manager.Event())
                        )

                        # multiprocessing.apply_async
                        # input args:
                        #   func, args: list, kwargs: dict, *, callback, error_callback
                        # output:
                        #   async_result
                        pool.apply_async(
                            _create_regfile_func,
                            (reginf, prev_reginf),
                            {"hardlink_event": event},
                            error_callback=error_callback,
                        )
                    else:
                        pool.apply_async(
                            _create_regfile_func,
                            (reginf,),
                            error_callback=error_callback,
                        )

                pool.close()
                while len(processed_list) < len(reginf_list_raw_lines):
                    self._set_statistics(processed_list)

                    if not error_queue.empty():
                        error = error_queue.get()
                        pool.terminate()
                        raise error
                    time.sleep(2)  # set pulling interval to 2 seconds
                self._set_statistics(processed_list)

    # NOTE:
    # _create_regular_file should be static to be used from pool.apply_async,
    # since self._lock can't be pickled.
    @staticmethod
    def _create_regular_file(
        reginf: RegularInf,
        prev_reginf: RegularInf = None,
        *,
        # required options
        url_base: str,
        cookies: dict,
        standby_path: Path,
        processed_list,
        boot_standby_path: Path,
        # for hardlink file
        hardlink_event=None,
    ):
        processed = {}
        begin_time = time.time()
        ishardlink = reginf.nlink >= 2
        hardlink_first_copy = (
            prev_reginf is not None and prev_reginf.path == reginf.path
        )

        if str(reginf.path).startswith("/boot"):
            dst = boot_standby_path / reginf.path.name
        else:
            dst = standby_path / reginf.path.relative_to("/")

        if ishardlink and not hardlink_first_copy:
            # wait until the first copy is ready
            hardlink_event.wait()
            (standby_path / prev_reginf.path.relative_to("/")).link_to(dst)
            processed["op"] = "link"
            processed["errors"] = 0
        else:  # normal file or first copy of hardlink file
            if reginf.path.is_file() and verify_file(reginf.path, reginf.sha256hash):
                # copy file from active bank if hash is the same
                shutil.copy(reginf.path, dst)
                processed["op"] = "copy"
                processed["errors"] = 0
            else:
                url_path = urllib.parse.quote(str(reginf.path.relative_to("/")))
                url = urllib.parse.urljoin(url_base, url_path)
                errors = OtaClient._download(url, cookies, dst, reginf.sha256hash)
                processed["op"] = "download"
                processed["errors"] = errors
        processed["size"] = dst.stat().st_size

        os.chown(dst, reginf.uid, reginf.gid)
        os.chmod(dst, reginf.mode)

        end_time = time.time()
        processed["elapsed"] = end_time - begin_time

        processed_list.append(processed)
        if ishardlink and hardlink_first_copy:
            hardlink_event.set()  # first copy of hardlink file is ready

    def _copy_persistent_files(self, list_file, standby_path):
        copy_tree = CopyTree(
            src_passwd_file=self._passwd_file,
            src_group_file=self._group_file,
            dst_passwd_file=standby_path / self._passwd_file.relative_to("/"),
            dst_group_file=standby_path / self._group_file.relative_to("/"),
        )
        lines = open(list_file).read().splitlines()
        for line in lines:
            perinf = PersistentInf(line)
            if (
                perinf.path.is_file()
                or perinf.path.is_dir()
                or perinf.path.is_symlink()
            ):  # NOTE: not equivalent to perinf.path.exists()
                copy_tree.copy_with_parents(perinf.path, standby_path)


if __name__ == "__main__":
    ota_client = OtaClient()
    ota_client.update("123.x", "http://localhost:8080", "{}")
