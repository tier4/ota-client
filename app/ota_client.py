import dataclasses
import tempfile
import requests
import shutil
import re
import os
import time
import json
from typing import Any, Dict, Tuple, Union
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from json.decoder import JSONDecodeError
from multiprocessing import Pool, Manager
from threading import Event, Lock
from functools import partial
from enum import Enum, unique
from urllib.parse import quote_from_bytes, urlparse, urljoin

from ota_client_interface import OtaClientInterface
from ota_metadata import OtaMetadata
from ota_status import OtaStatus, OtaStatusControlMixin
from ota_error import OtaErrorUnrecoverable, OtaErrorRecoverable, OtaErrorBusy
from copy_tree import CopyTree
from configs import OTAFileCacheControl, config as cfg
from proxy_info import proxy_cfg
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


def verify_file(filename: Path, filehash: str, filesize) -> bool:
    if filesize and filename.stat().st_size != filesize:
        return False
    return file_sha256(filename) == filehash


_ExceptionWrapper = type("_ExceptionWrapper", (Exception,), dict())


def _retry(retry, backoff_factor, backoff_max, func):
    """simple retrier"""
    from functools import wraps

    @wraps(func)
    def _wrapper(*args, **kwargs):
        _retry_count, _retry_cache = 0, False
        try:
            while True:
                try:
                    if _retry_cache:
                        # add a Ota-File-Cache-Control header to indicate ota_proxy
                        # to re-cache the possible corrupted file.
                        # modify header if needed and inject it into kwargs
                        if "headers" in kwargs:
                            kwargs["headers"].update(
                                {
                                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.retry_caching.value
                                }
                            )
                        else:
                            kwargs["headers"] = {
                                OTAFileCacheControl.header_lower.value: OTAFileCacheControl.retry_caching.value
                            }

                    # inject headers
                    return func(*args, **kwargs)
                except _ExceptionWrapper as e:
                    # unwrap exception
                    _inner_e = e.__cause__
                    _retry_count += 1

                    if _retry_count > retry:
                        raise _inner_e
                    else:
                        # special case: hash calculation error detected,
                        # might indicate corrupted cached files
                        if isinstance(_inner_e, ValueError):
                            _retry_cache = True

                        _backoff_time = float(
                            min(backoff_max, backoff_factor * (2 ** (_retry_count - 1)))
                        )
                        time.sleep(_backoff_time)
        except Exception as e:
            # currently all exceptions lead to OtaErrorRecoverable
            raise OtaErrorRecoverable(f"{e!r}")

    return _wrapper


class Downloader:
    CHUNK_SIZE = 1 * 1024 * 1024  # 1MB
    RETRY_COUNT = 5
    BACKOFF_FACTOR = 1
    OUTER_BACKOFF_FACTOR = 0.01
    BACKOFF_MAX = 10

    def __init__(self):
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        # base session
        session = requests.Session()

        # cleanup proxy if any
        self._proxy_set = False
        proxies = {"http": "", "https": ""}
        session.proxies.update(proxies)

        # init retry mechanism
        # NOTE: for urllib3 version below 2.0, we have to change Retry class' DEFAULT_BACKOFF_MAX,
        # to configure the backoff max, set the value to the instance will not work as increment() method
        # will create a new instance of Retry on every try without inherit the change to instance's DEFAULT_BACKOFF_MAX
        Retry.DEFAULT_BACKOFF_MAX = self.BACKOFF_MAX
        retry_strategy = Retry(
            total=self.RETRY_COUNT,
            raise_on_status=True,
            backoff_factor=self.BACKOFF_FACTOR,
            # retry on common server side errors and non-critical client side errors
            status_forcelist={413, 429, 500, 502, 503, 504},
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        # register the connection pool
        self._session = session

    def configure_proxy(self, proxy: str):
        # configure proxy
        self._proxy_set = True
        proxies = {"http": proxy, "https": ""}
        self._session.proxies.update(proxies)

    def cleanup_proxy(self):
        self._proxy_set = False
        self.configure_proxy("")

    def _path_to_url(self, base: str, p: Union[Path, str]) -> str:
        # regulate base url, add suffix / to it if not existed
        if not base.endswith("/"):
            base = f"{base}/"

        if isinstance(p, str):
            p = Path(p)

        relative_path = p
        # if the path is relative to /
        try:
            relative_path = p.relative_to("/")
        except ValueError:
            pass

        quoted_path = quote_from_bytes(bytes(relative_path))

        # switch scheme if needed
        _url_parsed = urlparse(urljoin(base, quoted_path))
        # unconditionally set scheme to HTTP if proxy is applied
        if self._proxy_set:
            _url_parsed = _url_parsed._replace(scheme="http")

        return _url_parsed.geturl()

    @partial(_retry, RETRY_COUNT, OUTER_BACKOFF_FACTOR, BACKOFF_MAX)
    def __call__(
        self,
        url_base: str,
        path: str,
        dst: Path,
        digest: str,
        cookies: Dict[str, str],
        headers: Dict[str, str] = None,
    ) -> int:
        url = self._path_to_url(url_base, path)
        if not headers:
            headers = dict()

        try:
            error_count = 0
            response = self._session.get(
                url, stream=True, cookies=cookies, headers=headers
            )
            response.raise_for_status()

            raw_r = response.raw
            if raw_r.retries:
                error_count = len(raw_r.retries.history)

            # prepare hash
            hash_f = sha256()
            with open(dst, "wb") as f:
                for data in response.iter_content(chunk_size=self.CHUNK_SIZE):
                    hash_f.update(data)
                    f.write(data)

            calc_digest = hash_f.hexdigest()
            if digest and calc_digest != digest:
                msg = f"hash check failed detected: act={calc_digest}, exp={digest}, {url=}"
                logger.error(msg)
                raise ValueError(msg)
        except Exception as e:
            # rewrap the exception with url
            raise _ExceptionWrapper(f"request failed for {url=}: {e}") from e

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

    _pattern = re.compile(
        r"(?P<nlink>\d+),(?P<hash>\w+),'(?P<path>.+)',?(?P<size>\d+)?"
    )

    def __init__(self, info):
        super().__init__(info)

        res = self._pattern.match(self._left)
        assert res is not None
        self.nlink = int(res.group("nlink"))
        self.sha256hash = res.group("hash")
        self.path = Path(self.de_escape(res.group("path")))
        # make sure that size might be None
        size = res.group("size")
        self.size = None if size is None else int(size)


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


@dataclasses.dataclass
class _OtaClientStatisticsStorage:
    total_regular_files: int = 0
    total_regular_file_size: int = 0
    regular_files_processed: int = 0
    files_processed_copy: int = 0
    files_processed_link: int = 0
    files_processed_download: int = 0
    file_size_processed_copy: int = 0
    file_size_processed_link: int = 0
    file_size_processed_download: int = 0
    elapsed_time_copy: int = 0
    elapsed_time_link: int = 0
    elapsed_time_download: int = 0
    errors_download: int = 0
    total_elapsed_time: int = 0

    def copy(self):
        return dataclasses.replace(self)

    def export_as_dict(self) -> dict:
        return dataclasses.asdict(self)

    def __getitem__(self, key) -> Any:
        return getattr(self, key)

    def __setitem__(self, key: str, value: Any):
        setattr(self, key, value)


class OtaClientStatistics(object):
    def __init__(self):
        self._lock = Lock()
        self._slot = _OtaClientStatisticsStorage()

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
            setattr(self._slot, attr, value)

    def clear(self):
        """
        clear the storage slot and reset to empty
        """
        self._slot = _OtaClientStatisticsStorage()

    @contextmanager
    def acquire_staging_storage(self):
        """
        acquire a staging storage for updating the slot atomically and thread-safely
        """
        try:
            self._lock.acquire()
            staging_slot: _OtaClientStatisticsStorage = self._slot.copy()
            yield staging_slot
        finally:
            self._slot = staging_slot
            self._lock.release()


class OtaStateSync:
    # states definition
    _START, _S1, _S2, _END = (
        "start",
        "pre_update_finished",
        "apply_update_finished",
        "end",
    )
    # participators definition
    _P1, _P2 = "ota_service", "ota_client"
    # which participator can start the fsm
    _STARTER = _P1

    __slots__ = ("_start", "_s1", "_s2", "_end", "_map")

    def __init__(self):
        self._start = Event()
        self._s1 = Event()
        self._s2 = Event()
        self._end = Event()

        self._map = {
            self._START: self._start,
            self._S1: self._s1,
            self._S2: self._s2,
            self._END: self._end,
        }

    def start(self, caller: str):
        if caller != self._STARTER:
            raise RuntimeError(
                f"unexpected {caller=} start status machine, expect {self._P1}"
            )

        if not self._start.is_set():
            self._start.set()

    def _state_selector(self, caller, *, state) -> Tuple[Event, Event, str]:
        """logic of state machine"""
        cur_event, next_event, next_state = None, None, None
        # ota_client finishes pre_update procedure,
        # signal ota_service to send update requests to all subecus
        if state == self._START and caller == self._P2:
            cur_event = self._start
            next_event = self._s1
            next_state = self._S1
        # ota_client finishes local update,
        # signal ota_service to cleanup after all subecus are ready
        elif state == self._S1 and caller == self._P2:
            cur_event = self._s1
            next_event = self._s2
            next_state = self._S2
        # ota_service finishes cleaning up,
        # signal ota_client to reboot
        elif state == self._S2 and caller == self._P1:
            cur_event = self._s2
            next_event = self._end
            next_state = self._END
        else:
            raise RuntimeError(f"unexpected {caller=} or {state=}")

        return cur_event, next_event, next_state

    def wait_on(self, state: str, *, timeout: float = None) -> bool:
        return self._map[state].wait(timeout=timeout)

    @contextmanager
    def proceed(self, caller, *, expect, timeout: float = None) -> int:
        _wait_on, _next, _next_state = self._state_selector(caller, state=expect)

        if not _wait_on.wait(timeout=timeout):
            raise TimeoutError(f"timeout waiting state={expect}")

        try:
            yield _next_state
        finally:
            # after finish working, switch state
            if not _next.is_set():
                _next.set()
            else:
                raise RuntimeError(f"expect {_next_state=} not being set yet")


class _BaseOtaClient(OtaStatusControlMixin, OtaClientInterface):
    def __init__(self):
        self._lock = Lock()  # NOTE: can't be referenced from pool.apply_async target.
        self._failure_type = OtaClientFailureType.NO_FAILURE
        self._failure_reason = ""
        self._update_phase = OtaClientUpdatePhase.INITIAL
        self._update_start_time: int = 0  # unix time in milli-seconds

        self._mount_point = Path(cfg.MOUNT_POINT)
        self._passwd_file = Path(cfg.PASSWD_FILE)
        self._group_file = Path(cfg.GROUP_FILE)

        # statistics
        self._statistics = OtaClientStatistics()

        # downloader
        self._download = Downloader()

    def update(
        self,
        version,
        url_base,
        cookies_json: str,
        *,
        fsm: OtaStateSync = None,
    ):
        """
        main entry of the ota update logic
        exceptions are captured and recorded here
        """
        logger.debug("[update] entering...")

        try:
            cookies = json.loads(cookies_json)
            self._update(version, url_base, cookies, fsm=fsm)
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

    def _update(
        self,
        version,
        url_base,
        cookies,
        *,
        fsm: OtaStateSync,
    ):
        logger.info(f"{version=},{url_base=},{cookies=}")
        """
        e.g.
        cookies = {
            "CloudFront-Policy": "eyJTdGF0ZW1lbnQ...",
            "CloudFront-Signature": "o4ojzMrJwtSIg~izsy...",
            "CloudFront-Key-Pair-Id": "K2...",
        }
        """

        # set the status for ota-updating
        with self._lock:
            self.check_update_status()

            # set ota status
            self.set_ota_status(OtaStatus.UPDATING)
            # set update status
            self._update_phase = OtaClientUpdatePhase.INITIAL
            self._failure_type = OtaClientFailureType.NO_FAILURE
            self._update_start_time = int(time.time() * 1000)
            self._failure_reason = ""
            self._statistics.clear()

        if fsm:
            with fsm.proceed(fsm._P2, expect=fsm._START):
                logger.debug("ota_client: signal ota_stub that pre_update finished")

        proxy = proxy_cfg.get_proxy_for_local_ota()
        if proxy:
            self._download.configure_proxy(proxy)
            # FIX: wait for local ota cache scrubing finish
            # ota_proxy will not be functional before
            # TODO: implement a state from this on state machgine
            t = 32
            logger.info(f"sleep for {t}secs to wait for ota cache scrubbing...")
            time.sleep(t)

        # pre-update
        self.enter_update(version)

        # process metadata.jwt
        logger.debug("[update] process metadata...")
        self._update_phase = OtaClientUpdatePhase.METADATA
        url = f"{url_base}/"
        metadata = self._process_metadata(url, cookies)
        total_regular_file_size = metadata.get_total_regular_file_size()
        if total_regular_file_size:
            self._statistics.set("total_regular_file_size", total_regular_file_size)

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

        # standby slot preparation finished, set phase to POST_PROCESSING
        logger.info("[update] update finished, entering post-update...")
        self._update_phase = OtaClientUpdatePhase.POST_PROCESSING

        # finish update, we reset the downloader's proxy setting
        self._download.cleanup_proxy()

        if fsm:
            with fsm.proceed(fsm._P2, expect=fsm._S1):
                logger.debug("[update] signal ota_service that local update finished")

        logger.debug("[update] leaving update, wait on ota_service and then reboot...")
        if fsm:
            fsm.wait_on(fsm._END)
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
        if self.get_ota_status() == OtaStatus.UPDATING:
            total_elapsed_time = int(time.time() * 1000) - self._update_start_time
            self._statistics.set("total_elapsed_time", total_elapsed_time)
        update_progress = self._statistics.get_snapshot().export_as_dict()
        # add extra fields
        update_progress["phase"] = self._update_phase.name

        return {
            "status": self.get_ota_status().name,
            "failure_type": self._failure_type.name,
            "failure_reason": self._failure_reason,
            "version": self.get_version(),
            "update_progress": update_progress,
        }

    def _verify_metadata(self, url_base, cookies, list_info, metadata):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            # NOTE: do not use cache when fetching metadata
            self._download(
                url_base,
                list_info["file"],
                file_name,
                list_info["hash"],
                cookies=cookies,
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                },
            )
            metadata.verify(open(file_name).read())
            logger.info("done")

    def _process_metadata(self, url_base, cookies: Dict[str, str]):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / "metadata.jwt"
            # NOTE: do not use cache when fetching metadata
            self._download(
                url_base,
                "metadata.jwt",
                file_name,
                None,
                cookies=cookies,
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                },
            )

            metadata = OtaMetadata(open(file_name, "r").read())
            certificate_info = metadata.get_certificate_info()
            self._verify_metadata(url_base, cookies, certificate_info, metadata)
            logger.info("done")
            return metadata

    def _process_directory(self, url_base, cookies, list_info, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            # NOTE: do not use cache when fetching dir list
            self._download(
                url_base,
                list_info["file"],
                file_name,
                list_info["hash"],
                cookies=cookies,
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                },
            )
            self._create_directories(file_name, standby_path)
            logger.info("done")

    def _process_symlink(self, url_base, cookies, list_info, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            # NOTE: do not use cache when fetching symlink list
            self._download(
                url_base,
                list_info["file"],
                file_name,
                list_info["hash"],
                cookies=cookies,
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                },
            )
            self._create_symbolic_links(file_name, standby_path)
            logger.info("done")

    def _process_regular(self, url_base, cookies, list_info, rootfsdir, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            # NOTE: do not use cache when fetching regular files list
            self._download(
                url_base,
                list_info["file"],
                file_name,
                list_info["hash"],
                cookies=cookies,
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                },
            )
            url_rootfsdir = urljoin(url_base, f"{rootfsdir}/")
            self._create_regular_files(url_rootfsdir, cookies, file_name, standby_path)
            logger.info("done")

    def _process_persistent(self, url_base, cookies, list_info, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            # NOTE: do not use cache when fetching persist files list
            self._download(
                url_base,
                list_info["file"],
                file_name,
                list_info["hash"],
                cookies=cookies,
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                },
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
            already_processed = staging_storage.regular_files_processed
            if already_processed >= staging_storage.total_regular_files:
                return

            staging_storage.regular_files_processed += all_processed - already_processed
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
        # NOTE: check _OtaStatisticsStorage for available attributes
        self._statistics.set("total_regular_files", len(reginf_list_raw_lines))

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
                downloader=self._download,
            )

            _max_workder = min(3, os.cpu_count())
            with Pool(processes=_max_workder) as pool:
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
        downloader,
    ):
        processed = {}
        begin_time = time.time()
        ishardlink = reginf.nlink >= 2
        hardlink_first_copy = (
            prev_reginf is not None and prev_reginf.path == reginf.path
        )

        if str(reginf.path).startswith("/boot"):
            dst = boot_standby_path / reginf.path.relative_to("/boot")
        else:
            dst = standby_path / reginf.path.relative_to("/")

        if ishardlink and not hardlink_first_copy:
            # wait until the first copy is ready
            hardlink_event.wait()
            (standby_path / prev_reginf.path.relative_to("/")).link_to(dst)
            processed["op"] = "link"
            processed["errors"] = 0
        else:  # normal file or first copy of hardlink file
            if reginf.path.is_file() and verify_file(
                reginf.path, reginf.sha256hash, reginf.size
            ):
                # copy file from active bank if hash is the same
                shutil.copy(reginf.path, dst)
                processed["op"] = "copy"
                processed["errors"] = 0
            else:
                processed["errors"] = downloader(
                    url_base,
                    reginf.path,
                    dst,
                    reginf.sha256hash,
                    cookies=cookies,
                )
                processed["op"] = "download"

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
        self.store_standby_ota_status(OtaStatus.UPDATING)
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


def gen_ota_client_class(bootloader: str):
    if bootloader == "grub":

        from grub_ota_partition import GrubControlMixin, OtaPartitionFile

        class OtaClient(_BaseOtaClient, GrubControlMixin):
            def __init__(self):
                super().__init__()

                self._boot_control: OtaPartitionFile = OtaPartitionFile()
                self._ota_status: OtaStatus = self.initialize_ota_status()

                logger.debug(f"ota status: {self._ota_status.name}")

    elif bootloader == "cboot":

        from extlinux_control import CBootControl, CBootControlMixin

        class OtaClient(_BaseOtaClient, CBootControlMixin):
            def __init__(self):
                super().__init__()

                # current slot
                self._ota_status_dir: Path = Path(cfg.OTA_STATUS_DIR)
                self._ota_status_file: Path = (
                    self._ota_status_dir / cfg.OTA_STATUS_FNAME
                )
                self._ota_version_file: Path = (
                    self._ota_status_dir / cfg.OTA_VERSION_FNAME
                )
                self._slot_in_use_file: Path = Path(cfg.SLOT_IN_USE_FILE)

                # standby slot
                self._standby_ota_status_dir: Path = (
                    self._mount_point / self._ota_status_dir.relative_to("/")
                )
                self._standby_ota_status_file = (
                    self._standby_ota_status_dir / cfg.OTA_STATUS_FNAME
                )
                self._standby_ota_version_file = (
                    self._standby_ota_status_dir / cfg.OTA_VERSION_FNAME
                )
                self._standby_slot_in_use_file = (
                    self._mount_point / self._slot_in_use_file.relative_to("/")
                )

                # standby bootdev
                self._standby_boot_mount_point = Path(cfg.SEPARATE_BOOT_MOUNT_POINT)

                self._boot_control: CBootControl = CBootControl()
                self._ota_status: OtaStatus = self.initialize_ota_status()
                self._slot_in_use = self.load_slot_in_use_file()

                logger.info(f"ota status: {self._ota_status.name}")

    return OtaClient


def _ota_client_class():
    bootloader = cfg.BOOTLOADER
    logger.debug(f"ota_client is running with {bootloader=}")

    return gen_ota_client_class(bootloader)


OtaClient = _ota_client_class()

if __name__ == "__main__":
    ota_client = OtaClient()
    ota_client.update("123.x", "http://localhost:8080", "{}")
