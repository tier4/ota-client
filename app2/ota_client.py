import tempfile
from pathlib import Path
import requests
from hashlib import sha256
import shutil
import urllib.parse
import re
import os
import time
from multiprocessing import Pool, Manager
import subprocess
import shlex
from logging import getLogger

from ota_status import OtaStatusControl
from ota_metadata import OtaMetaData
import configs as cfg

logger = getLogger(__name__)
logger.setLevel(cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL))


def file_sha256(filename) -> str:
    with open(filename, "rb") as f:
        return sha256(f.read()).hexdigest()


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


class OtaClient:
    MOUNT_POINT = cfg.MOUNT_POINT  # Path("/mnt/standby")

    def __init__(self):
        self._ota_status = OtaStatusControl()
        self._mount_point = OtaClient.MOUNT_POINT

    def update(self, version, url_base, cookies):
        """
        cookies = {
            "CloudFront-Policy": "eyJTdGF0ZW1lbnQ...",
            "CloudFront-Signature": "o4ojzMrJwtSIg~izsy...",
            "CloudFront-Key-Pair-Id": "K2...",
        }
        """
        # enter update
        self._ota_status.enter_updating(version, self._mount_point)

        # process metadata.jwt
        url = f"{url_base}/"
        metadata = self._process_metadata(url, cookies)

        # process directory file
        self._process_directory(
            url, cookies, metadata.get_directories_info(), self._mount_point
        )

        # process symlink file
        self._process_symlink(
            url, cookies, metadata.get_symboliclinks_info(), self._mount_point
        )

        # process regular file
        self._process_regular(
            url,
            cookies,
            metadata.get_regulars_info(),
            metadata.get_rootfsdir_info()["file"],
            self._mount_point,
        )

        # process persistent file
        self._process_persistent(
            url, cookies, metadata.get_persistent_info(), self._mount_point
        )

        # leave update
        self._ota_status.leave_updating(self._mount_point)

    def rollback(self):
        # enter rollback
        self._ota_status.enter_rollbacking()
        # leave rollback
        self._ota_status.leave_rollbacking()

    def status(self):
        return {
            "status": self._ota_status.get_status(),
            "failure_type": self._ota_status.get_failure_type(),
            "failure_reason": self._ota_status.get_failure_reason(),
            "version": self._ota_status._get_version(),
            "update_progress": {  # TODO
                "phase": "",
                "total_regular_files": 0,
                "regular_files_processed": 0,
            },
            "rollback_progress": {  # TODO
                "phase": "",
            },
        }

    """ private functions from here """

    def _download(self, url, cookies, dst, digest, retry=5):
        def _requests_get():
            headers = {}
            headers["Accept-encording"] = "gzip"
            # TODO: cookies=cookies is not tested yet.
            response = requests.get(url, headers=headers, cookies=cookies, timeout=10)
            # For 50x, especially 503, wait a few seconds and try again.
            if response.status_code / 100 == 5:
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
                    raise

        with open(dst, "wb") as f:
            m = sha256()
            total_length = response.headers.get("content-length")
            if total_length is None:
                m.update(response.content)
                f.write(response.content)
            else:
                dl = 0
                total_length = int(total_length)
                for data in response.iter_content(chunk_size=4096):
                    dl += len(data)
                    m.update(data)
                    f.write(data)

        calc_digest = m.hexdigest()
        if digest and digest != calc_digest:
            raise ValueError(f"hash error: act={calc_digest}, exp={digest}")

    def _process_metadata(self, url_base, cookies):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / "metadata.jwt"
            url = urllib.parse.urljoin(url_base, "metadata.jwt")
            self._download(url, cookies, file_name, None)
            # TODO: verify metadata
            return OtaMetaData(open(file_name, "r").read())

    def _process_directory(self, url_base, cookies, list_info, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            url = urllib.parse.urljoin(url_base, list_info["file"])
            self._download(url, cookies, file_name, list_info["hash"])
            self._create_directories(file_name, standby_path)

    def _process_symlink(self, url_base, cookies, list_info, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            url = urllib.parse.urljoin(url_base, list_info["file"])
            self._download(url, cookies, file_name, list_info["hash"])
            self._create_symbolic_links(file_name, standby_path)

    def _process_regular(self, url_base, cookies, list_info, rootfsdir, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            url = urllib.parse.urljoin(url_base, list_info["file"])
            self._download(url, cookies, file_name, list_info["hash"])
            url_rootfsdir = urllib.parse.urljoin(url_base, f"{rootfsdir}/")
            self._create_regular_files(url_rootfsdir, cookies, file_name, standby_path)

    def _process_persistent(self, url_base, cookies, list_info, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            url = urllib.parse.urljoin(url_base, list_info["file"])
            self._download(url, cookies, file_name, list_info["hash"])
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

    def _create_regular_files(self, url_base, cookies, list_file, standby_path):
        lines = open(list_file).read().splitlines()
        reginf_list = [RegularInf(l) for l in lines]
        with Manager() as manager:
            hardlink_dict = manager.dict()
            error_queue = manager.Queue()
            # NOTE: manager.Value doesn't work properly.
            processed_list = manager.list()

            def error_callback(e):
                error_queue.put(e)

            with Pool() as pool:
                self._create_regular_files_with_async(
                    url_base,
                    cookies,
                    standby_path,
                    reginf_list,
                    hardlink_dict,
                    processed_list,
                    pool.apply_async,
                    error_callback,
                )
                pool.close()
                while len(processed_list) < len(reginf_list):
                    if not error_queue.empty():
                        error = error_queue.get()
                        pool.terminate()
                        raise error
                    time.sleep(2)

    def _create_regular_files_with_async(
        self,
        url_base,
        cookies,
        standby_path,
        reginf_list,
        hardlink_dict,
        processed_list,
        async_call,
        error_callback,
    ):
        for reginf in reginf_list:
            if reginf.nlink >= 2 and hardlink_dict.get(reginf.sha256hash) is None:
                try:
                    self._create_regular_file(
                        url_base,
                        cookies,
                        reginf,
                        standby_path,
                        hardlink_dict,
                        processed_list,
                    )
                except Exception as e:
                    error_callback(e)
            else:
                async_call(
                    self._create_regular_file,
                    (
                        url_base,
                        cookies,
                        reginf,
                        standby_path,
                        hardlink_dict,
                        processed_list,
                    ),
                    error_callback=error_callback,
                )

    def _create_regular_file(
        self,
        url_base,
        cookies,
        reginf,
        standby_path,
        hardlink_dict,
        processed_list,
    ):
        url = urllib.parse.urljoin(url_base, str(reginf.path.relative_to("/")))
        if str(reginf.path).startswith("/boot"):
            boot_standby_path = self._ota_status.get_standby_boot_partition_path()
            dst = boot_standby_path / reginf.path.name
        else:
            dst = standby_path / reginf.path.relative_to("/")

        hardlink_path = hardlink_dict.get(reginf.sha256hash)
        if hardlink_path:
            # link file
            hardlink_path.link_to(dst)
        elif reginf.path.is_file() and file_sha256(reginf.path) == reginf.sha256hash:
            # copy file form active bank if hash is the same
            shutil.copy(reginf.path, dst)
        else:
            self._download(url, cookies, dst, reginf.sha256hash)

        if reginf.nlink >= 2:
            # save hardlink path
            hardlink_dict.setdefault(reginf.sha256hash, dst)

        os.chown(dst, reginf.uid, reginf.gid)
        os.chmod(dst, reginf.mode)

        processed_list.append(True)

    def _copy_persistent_files(self, list_file, standby_path):
        lines = open(list_file).read().splitlines()
        for l in lines:
            perinf = PersistentInf(l)
            if (
                perinf.path.is_file()
                or perinf.path.is_dir()
                or perinf.path.is_symlink()
            ):  # NOTE: not equivalent to perinf.path.exists()
                self._cp_cmd(perinf.path, standby_path)

    def _cp_cmd(self, src, standby_path):
        cmd = f"cp --parents -d -r -p {src} {standby_path}"
        return subprocess.check_output(shlex.split(cmd)).decode().strip()


if __name__ == "__main__":
    ota_client = OtaClient()
    ota_client.update("123.x", "http://localhost:8080", "")
