import tempfile
from pathlib import Path
import requests
from hashlib import sha256
import shutil
import urllib.parse

from ota_status import OtaStatusControl
from ota_metadata import OtaMetaData


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
        # process symlink file
        # process regular file
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

    def _download(self, url, header, dst, retry=5):
        header = header
        header["Accept-encording"] = "gzip"
        response = requests.get(url, headers=header, timeout=10)
        if response.status_code != 200:
            return response, ""

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
        shutil.move(temp_name, dst)
        return response, m.hexdigest()

    def _process_metadata(self, url, header):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / "metadata.jwt"
            self._download(urllib.parse.urljoin(url, "metadata.jwt"), header, file_name)
            return OtaMetaData(open(file_name, "r").read())

    def _process_directory(self, url, header, list_file, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d.name) / list_file
            self._download(url, header, file_name)
            self._create_directories(file_name, standby_path)

    def _process_symlink(self, url, header, list_file, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_file
            self._download(url, header, file_name)
            self._create_symbolic_links(file_name, standby_path)

    def _process_regular(self, url, header, list_file):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / list_file
            self._download(url, header, file_name)
            self._create_regular_files(file_name, standby_path)

    def _create_directories(self, list_file, standby_path):
        pass

    def _create_symbolic_links(self, list_file, standby_path):
        pass

    def _create_regular_files(self, list_file, standby_path):
        pass


if __name__ == "__main__":
    ota_client = OtaClient()
