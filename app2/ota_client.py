class OtaClient:
    def __init__(self):
        self._ota_status = OtaStatusControl()
        self._mount_porint = "/mnt/standby"

    def update(self, version, url, cookies):
        self._ota_status.enter_updating(version, self._mount_point)
        header = _cookies_to_header(cookies)
        # process metadata.jwt
        metadata = self._process_metadata(url, cookies)
        # process directory file
        # process symlink file
        # process regular file
        self._ota_status.leave_updating()
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

    def _download(url, header, dst, retry=5):
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
                target_file.write(response.content)
            else:
                dl = 0
                total_length = int(total_length)
                for data in response.iter_content(chunk_size=4096):
                    dl += len(data)
                    m.update(data)
                    target_file.write(data)
        shutil.move(temp_name, dst)
        return response, m.hexdigest()

    def _process_metadata(url, header):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = pathlib.Path(d.name) / "metadata.jwt"
            _download(url, header, file_name)
            return OtaMetaData(file_name)

    def _process_directory(url, header, list_file, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = pathlib.Path(d.name) / list_file
            _download(url, header, file_name)
            _create_directories(file_name, standby_path)

    def _process_symlink(url, header, list_file, standby_path):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = pathlib.Path(d.name) / list_file
            _download(url, header, file_name)
            _create_symbolic_links(file_name, standby_path):

    def _process_regular(url, header, list_file):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = pathlib.Path(d.name) / list_file
            _download(url, header, file_name)
            _create_regular_files(file_name, standby_path):

    def _create_directories(list_file, standby_path):
        pass

    def _create_symbolic_links(list_file, standby_path):
        pass

    def _create_regular_files(list_file, standby_path):
        pass


