import tempfile
import requests
import shutil
import urllib.parse
import re
import os
import time
import json
from hashlib import sha256
from pathlib import Path
from json.decoder import JSONDecodeError
from multiprocessing import Pool, Manager
from threading import Lock
from functools import partial
from enum import Enum, unique
from concurrent.futures import ThreadPoolExecutor

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
        self._update_total_regular_files = 0
        self._update_regular_files_processed = 0

    def update(self, version, url_base, cookies_json: str):
        # TODO: handle another update call when an update is on-the-flight?
        try:
            cookies = json.loads(cookies_json)
            self._update(version, url_base, cookies)
        except JSONDecodeError as e: # if cookie is invalid
            self._ota_status.set_ota_status(OtaStatus.FAILURE)
            return self._result_recoverable(e)
        except OtaErrorRecoverable as e:
            self._ota_status.set_ota_status(OtaStatus.FAILURE)
            return self._result_recoverable(e)
        except (OtaErrorUnrecoverable, Exception) as e:
            self._ota_status.set_ota_status(OtaStatus.FAILURE)
            return self._result_unrecoverable(e)

    def rollback(self):
        try:
            self._rollback()
            return self._result_ok()
        except OtaErrorRecoverable as e:
            self._ota_status.set_ota_status(OtaStatus.ROLLBACK_FAILURE)
            return self._result_recoverable(e)
        except (OtaErrorUnrecoverable, Exception) as e:
            self._ota_status.set_ota_status(OtaStatus.ROLLBACK_FAILURE)
            return self._result_unrecoverable(e)

    # NOTE: status should not update any internal status
    def status(self):
        try:
            status = self._status()
            return OtaClientFailureType.NO_FAILURE, status
        except OtaErrorRecoverable:
            return OtaClientFailureType.RECOVERABLE, None
        except (OtaErrorUnrecoverable, Exception):
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
        """
        e.g.
        cookies = {
            "CloudFront-Policy": "eyJTdGF0ZW1lbnQ...",
            "CloudFront-Signature": "o4ojzMrJwtSIg~izsy...",
            "CloudFront-Key-Pair-Id": "K2...",
        }
        """
        self._update_phase = OtaClientUpdatePhase.INITIAL
        self._total_regular_files = 0
        self._regular_files_processed = 0

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
                "total_regular_files": self._total_regular_files,
                "regular_files_processed": self._regular_files_processed,
            },
        }

    @property
    def _total_regular_files(self):
        with self._lock:
            return self._update_total_regular_files

    @property
    def _regular_files_processed(self):
        with self._lock:
            return self._update_regular_files_processed

    @_total_regular_files.setter
    def _total_regular_files(self, num):
        with self._lock:
            self._update_total_regular_files = num

    @_regular_files_processed.setter
    def _regular_files_processed(self, num):
        with self._lock:
            self._update_regular_files_processed = num

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
            try:
                response = _requests_get()
                break
            except Exception as e:
                count += 1
                if count == retry:
                    logger.exception(e)
                    # TODO: timeout or status_code information
                    raise OtaErrorRecoverable("requests error")

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
            raise OtaErrorRecoverable(f"hash error: act={calc_digest}, exp={digest}")

    def _verify_metadata(self, url_base, cookies, list_info, metadata):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            url = urllib.parse.urljoin(url_base, list_info["file"])
            OtaClient._download(url, cookies, file_name, list_info["hash"])
            metadata.verify(open(file_name).read())

    def _process_metadata(self, url_base, cookies):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / "metadata.jwt"
            url = urllib.parse.urljoin(url_base, "metadata.jwt")
            OtaClient._download(url, cookies, file_name, None)
            metadata = OtaMetadata(open(file_name, "r").read())
            certificate_info = metadata.get_certificate_info()
            self._verify_metadata(url_base, cookies, certificate_info, metadata)
            return metadata

    def _process_directory(self, url_base, cookies, list_info, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            url = urllib.parse.urljoin(url_base, list_info["file"])
            OtaClient._download(url, cookies, file_name, list_info["hash"])
            self._create_directories(file_name, standby_path)

    def _process_symlink(self, url_base, cookies, list_info, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            url = urllib.parse.urljoin(url_base, list_info["file"])
            OtaClient._download(url, cookies, file_name, list_info["hash"])
            self._create_symbolic_links(file_name, standby_path)

    def _process_regular(self, url_base, cookies, list_info, rootfsdir, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            url = urllib.parse.urljoin(url_base, list_info["file"])
            OtaClient._download(url, cookies, file_name, list_info["hash"])
            url_rootfsdir = urllib.parse.urljoin(url_base, f"{rootfsdir}/")
            self._create_regular_files(url_rootfsdir, cookies, file_name, standby_path)

    def _process_persistent(self, url_base, cookies, list_info, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            url = urllib.parse.urljoin(url_base, list_info["file"])
            OtaClient._download(url, cookies, file_name, list_info["hash"])
            self._copy_persistent_files(file_name, standby_path)

    def _create_directories(self, list_file, standby_path):
        lines = open(list_file).read().splitlines()
        for l in lines:
            dirinf = DirectoryInf(l)
            target_path = standby_path.joinpath(dirinf.path.relative_to("/"))
            target_path.mkdir(mode=dirinf.mode, parents=True, exist_ok=True)
            os.chown(target_path, dirinf.uid, dirinf.gid)
            os.chmod(target_path, dirinf.mode)

    def _create_symbolic_links(self, list_file, standby_path):
        lines = open(list_file).read().splitlines()
        for l in lines:
            # NOTE: symbolic link in /boot directory is not supported. We don't use it.
            slinkf = SymbolicLinkInf(l)
            slink = standby_path.joinpath(slinkf.slink.relative_to("/"))
            slink.symlink_to(slinkf.srcpath)
            os.chown(slink, slinkf.uid, slinkf.gid, follow_symlinks=False)

    def _create_regular_files(self, url_base: str, cookies, list_file, standby_path):
        reginf_list_raw_lines = open(list_file).read().splitlines()
        self._total_regular_files = len(reginf_list_raw_lines)

        with Manager() as manager:
            error_queue = manager.Queue()
            # NOTE: manager.Value doesn't work properly.
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

                # imap_unordered return a lazy itorator without blocking
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
                    self._regular_files_processed = len(processed_list)  # via setter
                    if not error_queue.empty():
                        error = error_queue.get()
                        pool.terminate()
                        raise error
                    time.sleep(2)  # set pulling interval to 2 seconds
                self._regular_files_processed = len(processed_list)  # via setter

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
        else:  # normal file or first copy of hardlink file
            if reginf.path.is_file() and verify_file(reginf.path, reginf.sha256hash):
                # copy file from active bank if hash is the same
                shutil.copy(reginf.path, dst)
            else:
                url = urllib.parse.urljoin(url_base, str(reginf.path.relative_to("/")))
                OtaClient._download(url, cookies, dst, reginf.sha256hash)

        os.chown(dst, reginf.uid, reginf.gid)
        os.chmod(dst, reginf.mode)

        processed_list.append(True)
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
        for l in lines:
            perinf = PersistentInf(l)
            if (
                perinf.path.is_file()
                or perinf.path.is_dir()
                or perinf.path.is_symlink()
            ):  # NOTE: not equivalent to perinf.path.exists()
                copy_tree.copy_with_parents(perinf.path, standby_path)


if __name__ == "__main__":
    ota_client = OtaClient()
    ota_client.update("123.x", "http://localhost:8080", "")
