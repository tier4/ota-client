import tempfile
import requests
import shutil
import urllib.parse
import re
import os
import time
import json
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from json.decoder import JSONDecodeError
from multiprocessing import Pool, Manager
from threading import Lock
from functools import partial
from enum import Enum, unique
from requests.exceptions import RequestException
from retrying import retry

from ota_client_interface import OtaClientInterface
from ota_metadata import OtaMetadata
from ota_status import OtaStatus, OtaStatusControlMixin
from ota_error import OtaErrorUnrecoverable, OtaErrorRecoverable, OtaErrorBusy
from copy_tree import CopyTree
from configs import config as cfg
import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


def file_sha256(filename: Path) -> str:
    ONE_MB = 1048576
    with open(filename, "rb") as f:
        m = sha256()
        while True:
            d = f.read(ONE_MB)
            if d == b"":
                break
            m.update(d)
        return m.hexdigest()


def verify_file(filename: Path, filehash: str) -> bool:
    return file_sha256(filename) == filehash


def _download(url_base: str, path: str, dst: Path, digest: str, *, cookies):
    quoted_path = urllib.parse.quote(path)
    url = urllib.parse.urljoin(url_base, quoted_path)

    error_count = 0
    last_error = ""

    def retry_if_possible(e):
        nonlocal error_count
        nonlocal last_error
        error_count += 1
        do_retry = True
        if isinstance(e, requests.exceptions.Timeout):
            last_error = f"requests timeout: {e},{url=} ({error_count})"
        elif isinstance(e, requests.exceptions.ConnectionError):
            last_error = f"requests connection error: {e},{url=} ({error_count})"
        elif isinstance(e, requests.exceptions.ChunkedEncodingError):
            last_error = f"requests ChunkedEncodingError: {url=} ({error_count})"
        elif isinstance(e, RequestException):
            status_code = getattr(e.response, "status_code", None)
            last_error = f"requests error: {status_code=},{url=} ({error_count})"
            if isinstance(status_code, int) and status_code // 100 == 4:
                do_retry = False
        else:
            last_error = f"requests error unknown: {e},{url=}, ({error_count})"
        logger.warning(last_error)
        return do_retry

    @retry(
        stop_max_attempt_number=5,
        retry_on_exception=retry_if_possible,
        wait_exponential_multiplier=1_000,
        wait_exponential_max=10_000,
    )
    def _requests_get():
        CHUNK_SIZE = 4096
        headers = {}
        headers["Accept-encording"] = "gzip"
        response = requests.get(
            url, headers=headers, cookies=cookies, timeout=10, stream=True
        )
        response.raise_for_status()

        with open(dst, "wb") as f:
            m = sha256()
            total_length = response.headers.get("content-length")
            if total_length is None:
                m.update(response.content)
                f.write(response.content)
            else:
                for data in response.iter_content(chunk_size=CHUNK_SIZE):
                    m.update(data)
                    f.write(data)
        return m.hexdigest()

    try:
        calc_digest = _requests_get()
    except Exception:
        logger.exception("requests_get")
        raise OtaErrorRecoverable(last_error)

    if digest and digest != calc_digest:
        raise OtaErrorRecoverable(
            f"hash error: act={calc_digest}, exp={digest}, {url=}"
        )

    return error_count


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
        self._lock = Lock()
        self._slot = self._init_statistics_storage()

    @staticmethod
    def _init_statistics_storage() -> dict:
        return {
            "total_files": 0,
            "files_processed": 0,
            "files_processed_copy": 0,
            "files_processed_link": 0,
            "files_processed_download": 0,
            "file_size_processed_copy": 0,
            "file_size_processed_link": 0,
            "file_size_processed_download": 0,
            "elapsed_time_copy": 0,  # in milliseconds
            "elapsed_time_link": 0,  # in milliseconds
            "elapsed_time_download": 0,  # in milliseconds
            "errors_download": 0,
        }

    def get_snapshot(self):
        """
        return a copy of statistics storage
        """
        return self._slot.copy()

    def set(self, attr: str, value):
        """
        set a single attr in the slot
        """
        with self._lock:
            self._slot[attr] = value

    def clear(self):
        """
        clear the storage slot and reset to empty
        """
        self._slot = self._init_statistics_storage()

    @contextmanager
    def acquire_staging_storage(self):
        """
        acquire a staging storage for updating the slot atomically and thread-safely
        """
        try:
            self._lock.acquire()
            staging_slot: dict = self._slot.copy()
            yield staging_slot
        finally:
            self._slot = staging_slot.copy()
            staging_slot.clear()
            self._lock.release()


class _BaseOtaClient(OtaStatusControlMixin, OtaClientInterface):
    def __init__(self):
        self._lock = Lock()  # NOTE: can't be referenced from pool.apply_async target.
        self._failure_type = OtaClientFailureType.NO_FAILURE
        self._failure_reason = ""
        self._update_phase = OtaClientUpdatePhase.INITIAL

        self._mount_point = cfg.MOUNT_POINT
        self._passwd_file = cfg.PASSWD_FILE
        self._group_file = cfg.GROUP_FILE

        # statistics
        self._statistics = OtaClientStatistics()

    def update(self, version, url_base, cookies_json: str, event=None):
        logger.debug("[update] entering...")
        try:
            cookies = json.loads(cookies_json)
            self._update(version, url_base, cookies, event)
            return self._result_ok()
        except OtaErrorBusy:  # there is an on-going update
            # not setting ota_status
            logger.exception("update busy")
            return OtaClientFailureType.RECOVERABLE
        except (JSONDecodeError, OtaErrorRecoverable) as e:
            logger.exception(msg="recoverable")
            self.set_ota_status(OtaStatus.FAILURE)
            self.store_standby_ota_status(OtaStatus.FAILURE)
            return self._result_recoverable(e)
        except (OtaErrorUnrecoverable, Exception) as e:
            logger.exception(msg="unrecoverable")
            self.set_ota_status(OtaStatus.FAILURE)
            self.store_standby_ota_status(OtaStatus.FAILURE)
            return self._result_unrecoverable(e)
        finally:
            if event:
                event.set()

    def rollback(self):
        try:
            self._rollback()
            return self._result_ok()
        except OtaErrorBusy:  # there is an on-going update
            # not setting ota_status
            logger.exception("rollback busy")
            return OtaClientFailureType.RECOVERABLE
        except OtaErrorRecoverable as e:
            logger.exception(msg="recoverable")
            self.set_ota_status(OtaStatus.ROLLBACK_FAILURE)
            self.store_standby_ota_status(OtaStatus.ROLLBACK_FAILURE)
            return self._result_recoverable(e)
        except (OtaErrorUnrecoverable, Exception) as e:
            logger.exception(msg="unrecoverable")
            self.set_ota_status(OtaStatus.ROLLBACK_FAILURE)
            self.store_standby_ota_status(OtaStatus.ROLLBACK_FAILURE)
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

    def _update(self, version, url_base, cookies, event=None):
        logger.info(f"{version=},{url_base=},{cookies=}")
        """
        e.g.
        cookies = {
            "CloudFront-Policy": "eyJTdGF0ZW1lbnQ...",
            "CloudFront-Signature": "o4ojzMrJwtSIg~izsy...",
            "CloudFront-Key-Pair-Id": "K2...",
        }
        """
        logger.debug("check if ota_status is valid for updating...")
        self.check_update_status()

        # set the status for ota-updating
        with self._lock:
            # set ota status
            self.set_ota_status(OtaStatus.UPDATING)
            self.store_standby_ota_status(OtaStatus.UPDATING)
            # set update status
            self._update_phase = OtaClientUpdatePhase.INITIAL
            self._failure_type = OtaClientFailureType.NO_FAILURE
            self._failure_reason = ""
            self._statistics.clear()
        if event:
            event.set()

        # pre-update
        self.enter_update(version)

        # process metadata.jwt
        logger.debug("[update] process metadata...")
        self._update_phase = OtaClientUpdatePhase.METADATA
        url = f"{url_base}/"
        metadata = self._process_metadata(url, cookies)

        # process directory file
        logger.debug("[update] process directory files...")
        self._update_phase = OtaClientUpdatePhase.DIRECTORY
        self._process_directory(
            url, cookies, metadata.get_directories_info(), self._mount_point
        )

        # process symlink file
        logger.debug("[update] process symlink files...")
        self._update_phase = OtaClientUpdatePhase.SYMLINK
        self._process_symlink(
            url, cookies, metadata.get_symboliclinks_info(), self._mount_point
        )

        # process regular file
        logger.debug("[update] process regular files...")
        self._update_phase = OtaClientUpdatePhase.REGULAR
        self._process_regular(
            url,
            cookies,
            metadata.get_regulars_info(),
            metadata.get_rootfsdir_info()["file"],
            self._mount_point,
        )

        # process persistent file
        logger.debug("[update] process persistent files...")
        self._update_phase = OtaClientUpdatePhase.PERSISTENT
        self._process_persistent(
            url, cookies, metadata.get_persistent_info(), self._mount_point
        )

        # leave update
        logger.debug("[update] update finished, entering post-update...")
        self._update_phase = OtaClientUpdatePhase.POST_PROCESSING
        self.leave_update()

    def _rollback(self):
        with self._lock:
            # enter rollback
            self.enter_rollback()
            self._failure_type = OtaClientFailureType.NO_FAILURE
            self._failure_reason = ""
        # leave rollback
        self.leave_rollback()

    def _status(self) -> dict:
        _statistics = self._statistics.get_snapshot()
        return {
            "status": self.get_ota_status().name,
            "failure_type": self._failure_type.name,
            "failure_reason": self._failure_reason,
            "version": self.get_version(),
            "update_progress": {
                "phase": self._update_phase.name,
                "total_regular_files": _statistics.get("total_files", 0),
                "regular_files_processed": _statistics.get("files_processed", 0),
                "files_processed_copy": _statistics.get("files_processed_copy", 0),
                "files_processed_link": _statistics.get("files_processed_link", 0),
                "files_processed_download": _statistics.get(
                    "files_processed_download", 0
                ),
                "file_size_processed_copy": _statistics.get(
                    "file_size_processed_copy", 0
                ),
                "file_size_processed_link": _statistics.get(
                    "file_size_processed_link", 0
                ),
                "file_size_processed_download": _statistics.get(
                    "file_size_processed_download", 0
                ),
                "elapsed_time_copy": _statistics.get("elapsed_time_copy", 0),
                "elapsed_time_link": _statistics.get("elapsed_time_link", 0),
                "elapsed_time_download": _statistics.get("elapsed_time_download", 0),
                "errors_download": _statistics.get("errors_download", 0),
            },
        }

    def _verify_metadata(self, url_base, cookies, list_info, metadata):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            _download(
                url_base,
                list_info["file"],
                file_name,
                list_info["hash"],
                cookies=cookies,
            )
            metadata.verify(open(file_name).read())
            logger.info("done")

    def _process_metadata(self, url_base, cookies):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / "metadata.jwt"
            _download(url_base, "metadata.jwt", file_name, None, cookies=cookies)

            metadata = OtaMetadata(open(file_name, "r").read())
            certificate_info = metadata.get_certificate_info()
            self._verify_metadata(url_base, cookies, certificate_info, metadata)
            logger.info("done")
            return metadata

    def _process_directory(self, url_base, cookies, list_info, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            _download(
                url_base,
                list_info["file"],
                file_name,
                list_info["hash"],
                cookies=cookies,
            )
            self._create_directories(file_name, standby_path)
            logger.info("done")

    def _process_symlink(self, url_base, cookies, list_info, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            _download(
                url_base,
                list_info["file"],
                file_name,
                list_info["hash"],
                cookies=cookies,
            )
            self._create_symbolic_links(file_name, standby_path)
            logger.info("done")

    def _process_regular(self, url_base, cookies, list_info, rootfsdir, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            _download(
                url_base,
                list_info["file"],
                file_name,
                list_info["hash"],
                cookies=cookies,
            )
            url_rootfsdir = urllib.parse.urljoin(url_base, f"{rootfsdir}/")
            self._create_regular_files(url_rootfsdir, cookies, file_name, standby_path)
            logger.info("done")

    def _process_persistent(self, url_base, cookies, list_info, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            _download(
                url_base,
                list_info["file"],
                file_name,
                list_info["hash"],
                cookies=cookies,
            )
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

    def _set_statistics(self, sts):
        """
        thread-safe modify statistics storage

        st format:
            {"size": int}  # file size
            {"elapsed": int}  # elapsed time in seconds
            {"op": str}  # operation. "copy", "link" or "download"
            {"errors": int}  # number of errors that occurred when downloading.
        """
        all_processed = len(sts)
        with self._statistics.acquire_staging_storage() as staging_storage:
            # NOTE: "files_processed" key and "total_files" key should be presented!
            already_processed = staging_storage["files_processed"]
            if already_processed >= staging_storage["total_files"]:
                return

            staging_storage["files_processed"] += all_processed - already_processed
            for st in sts[already_processed:all_processed]:
                _suffix = st.get("op")
                if _suffix in {"copy", "link", "download"}:
                    staging_storage[f"files_processed_{_suffix}"] += 1
                    staging_storage[f"file_size_processed_{_suffix}"] += st.get(
                        "size", 0
                    )
                    staging_storage[f"elapsed_time_{_suffix}"] += int(
                        st.get("elapsed", 0) * 1000
                    )
                    if _suffix == "download":
                        staging_storage[f"errors_{_suffix}"] += st.get("errors", 0)

    def _create_regular_files(self, url_base: str, cookies, list_file, standby_path):
        reginf_list_raw_lines = open(list_file).readlines()
        self._statistics.set("total_files", len(reginf_list_raw_lines))

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

            boot_standby_path = self.get_standby_boot_partition_path()
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
                hardlink_dict = dict()  # sha256hash[tuple[reginf, event]

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

                # final statistics record
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
        processed_list: list,
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
                errors = _download(
                    url_base,
                    str(reginf.path.relative_to("/")),
                    dst,
                    reginf.sha256hash,
                    cookies=cookies,
                )
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

    def enter_update(self, version):
        logger.debug("pre-update setup...")
        self.boot_ctrl_pre_update(version)
        logger.debug("finished pre-update setup")

    def leave_update(self):
        logger.debug("post-update setup...")
        self.boot_ctrl_post_update()

    def enter_rollback(self):
        self.check_rollback_status()
        self.set_ota_status(OtaStatus.ROLLBACKING)
        self.store_standby_ota_status(OtaStatus.ROLLBACKING)

    def leave_rollback(self):
        self.boot_ctrl_post_rollback()


def gen_ota_client_class(platform: str):
    if platform == "grub":

        from grub_ota_partition import GrubControlMixin, OtaPartitionFile

        class OtaClient(_BaseOtaClient, GrubControlMixin):
            def __init__(self):
                super().__init__()

                self._boot_control: OtaPartitionFile = OtaPartitionFile()
                self._ota_status: OtaStatus = self.initialize_ota_status()

                logger.debug(f"ota status: {self._ota_status.name}")

    elif platform == "cboot":

        from extlinux_control import CBootControl, CBootControlMixin

        class OtaClient(_BaseOtaClient, CBootControlMixin):
            def __init__(self):
                super().__init__()

                # current slot
                self._ota_status_dir: Path = cfg.OTA_STATUS_DIR
                self._ota_status_file: Path = (
                    self._ota_status_dir / cfg.OTA_STATUS_FNAME
                )
                self._ota_version_file: Path = (
                    self._ota_status_dir / cfg.OTA_VERSION_FNAME
                )
                self._slot_in_use_file: Path = cfg.SLOT_IN_USE_FILE

                # standby slot
                self._standby_ota_status_dir: Path = (
                    self._mount_point / self._ota_status_dir.relative_to(Path("/"))
                )
                self._standby_ota_status_file = (
                    self._standby_ota_status_dir / cfg.OTA_STATUS_FNAME
                )
                self._standby_ota_version_file = (
                    self._standby_ota_status_dir / cfg.OTA_VERSION_FNAME
                )
                self._standby_extlinux_cfg = (
                    self._mount_point / cfg.EXTLINUX_FILE.relative_to("/")
                )
                self._standby_slot_in_use_file = (
                    self._mount_point / self._slot_in_use_file.relative_to(Path("/"))
                )

                self._boot_control: CBootControl = CBootControl()
                self._ota_status: OtaStatus = self.initialize_ota_status()
                self._slot_in_use = self.load_slot_in_use_file()

                logger.info(f"ota status: {self._ota_status.name}")

    return OtaClient


def _ota_client_class():
    platform = cfg.PLATFORM
    logger.debug(f"ota_client is running with {platform}")

    return gen_ota_client_class(platform)


OtaClient = _ota_client_class()

if __name__ == "__main__":
    ota_client = OtaClient()
    ota_client.update("123.x", "http://localhost:8080", "{}")
