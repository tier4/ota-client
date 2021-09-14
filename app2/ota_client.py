import tempfile
from pathlib import Path
import requests
from hashlib import sha256
import shutil
import urllib.parse
import re
import os
import time

from ota_status import OtaStatusControl
from ota_metadata import OtaMetaData


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
    MOUNT_POINT = "/mnt/standby"

    def __init__(self):
        self._ota_status = OtaStatusControl()
        self._mount_point = OtaClient.MOUNT_POINT

    def update(self, version, url, cookies):
        self._ota_status.enter_updating(version, self._mount_point)

        header = self._cookies_to_header(cookies)

        # process metadata.jwt
        url = f"{url}/"
        metadata = self._process_metadata(url, header)

        # process directory file
        self._process_directory(
            url, header, metadata.get_directories_info(), self._mount_point
        )

        # process symlink file
        self._process_symlink(
            url, header, metadata.get_symboliclinks_info(), self._mount_point
        )

        # process regular file
        self._process_regular(
            url, header, metadata.get_regulars_info(), self._mount_point
        )

        self._ota_status.leave_updating(self._mount_point)
        # -> generate custom.cfg, grub-reboot and reboot internally

    def rollback(self):
        self._ota_status.enter_rollbacking()
        self._ota_status.leave_rollbacking()
        # -> generate custom.cfg, grub-reboot and reboot internally

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

    """ private from here """

    def _cookies_to_header(self, cookies):
        """"""
        header = {}
        for l in cookies.split(","):
            kl = l.split(":")
            if len(kl) == 2:
                header[kl[0]] = kl[1]
        return header

    def _download(self, url, header, dst, digest, retry=5):
        def _requests_get():
            headers = header
            headers["Accept-encording"] = "gzip"
            # TODO: `cookies=` can be used instead of `headers=`
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 503:
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

        with tempfile.NamedTemporaryFile("wb", delete=False, prefix=__name__) as f:
            temp_name = f.name
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
        shutil.move(temp_name, dst)

    def _process_metadata(self, url, header):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / "metadata.jwt"
            self._download(
                urllib.parse.urljoin(url, "metadata.jwt"), header, file_name, None
            )
            # TODO: verify metadata
            return OtaMetaData(open(file_name, "r").read())

    def _process_directory(self, url, header, list_info, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            file_url = urllib.parse.urljoin(url, list_info["file"])
            self._download(file_url, header, file_name, list_info["hash"])
            self._create_directories(file_name, standby_path)

    def _process_symlink(self, url, header, list_info, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            file_url = urllib.parse.urljoin(url, list_info["file"])
            self._download(file_url, header, file_name, list_info["hash"])
            self._create_symbolic_links(file_name, standby_path)

    def _process_regular(self, url, header, list_info, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_info["file"]
            file_url = urllib.parse.urljoin(url, list_info["file"])
            self._download(file_url, header, file_name, list_info["hash"])
            self._create_regular_files(file_name, standby_path)

    def _create_directories(self, list_file, standby_path):
        with open(list_file) as f:
            for l in f.read().splitlines():
                dirinf = DirectoryInf(l)
                target_path = standby_path.joinpath(dirinf.path.relative_to("/"))
                target_path.mkdir(mode=dirinf.mode, parents=True, exist_ok=True)
                os.chown(target_path, dirinf.uid, dirinf.gid)
                os.chmod(target_path, dirinf.mode)

    def _create_symbolic_links(self, list_file, standby_path):
        with open(list_file) as f:
            for l in f.read().splitlines():
                # NOTE: symbolic link in /boot directory is not supported. We don't use it.
                slinkf = SymbolicLinkInf(l)
                slink = standby_path.joinpath(slinkf.slink.relative_to("/"))
                slink.symlink_to(slinkf.srcpath)
                os.chown(
                    slink,
                    slinkf.uid,
                    slinkf.gid,
                    follow_symlinks=False,
                )

    def _create_regular_files(self, list_file, standby_path):
        pass


if __name__ == "__main__":
    ota_client = OtaClient()
