# Copyright 2022 TIER IV, INC. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Integration tests for the otaclient_common downloader.

The scope of these tests is the downloader subsystem talking to a real
HTTP server hosting a corpus of urandom blobs (some zstd-compressed).
The test HTTP server fixture and the corpus live in this subtree's
``conftest.py``.  Anything that can be tested without HTTP / threading
lives in the unit suite.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import threading
import time
from functools import partial
from hashlib import sha256
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing import Process
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import pytest_mock
import requests

from otaclient_common.downloader import (
    DEFAULT_RETRY_COUNT,
    DEFAULT_RETRY_STATUS,
    BrokenDecompressionError,
    Downloader,
    DownloaderPool,
    DownloadPoolWatchdogFuncContext,
    HashVerificationError,
    PartialDownload,
)
from otaclient_common.retry_task_map import ThreadPoolExecutorWithRetry

from .conftest import FileInfo

logger = logging.getLogger(__name__)


@pytest.fixture(autouse=True, scope="module")
def _serve_corpus(run_http_server_subprocess):
    """Keep the corpus HTTP server up for every test in this module.

    The underlying ``run_http_server_subprocess`` fixture lives in the
    shared conftest but is not ``autouse`` there (sibling modules don't
    need it); this module-scoped autouse wrapper opts the downloader
    suite back in.
    """
    yield


# ---------- single-instance Downloader against the live HTTP server ---------- #


class TestDownloaderAgainstLiveServer:
    @pytest.fixture(autouse=True)
    def setup_downloader(self, tmp_path: Path):
        self.downloader = Downloader(hash_func=sha256, chunk_size=4096)
        self._download_dir = tmp_path / "test_download_dir"
        self._download_dir.mkdir(exist_ok=True, parents=True)
        yield
        self.downloader.close()

    def test_download_full_corpus(
        self, setup_test_data: tuple[Path, list[FileInfo]]
    ) -> None:
        """Single-thread downloads of every file in the corpus succeed.

        Covers compressed + uncompressed blobs, URL escaping, and the
        size + digest verification path.
        """
        _, file_info_list = setup_test_data

        for file_info in file_info_list:
            self.downloader.download(
                file_info.url,
                self._download_dir / file_info.file_name,
                digest=file_info.sha256digest,
                size=file_info.size,
                compression_alg=file_info.compresson_alg,
            )
        assert self.downloader.downloaded_bytes > 0

    def test_download_returns_result_with_sane_fields(
        self, setup_test_data: tuple[Path, list[FileInfo]]
    ) -> None:
        _, file_info_list = setup_test_data
        # pick the first uncompressed file so we can compare on-wire and
        # download size 1:1 (compression makes traffic_on_wire < download_size)
        file_info = next(f for f in file_info_list if f.compresson_alg is None)
        result = self.downloader.download(
            file_info.url,
            self._download_dir / file_info.file_name,
            digest=file_info.sha256digest,
            size=file_info.size,
            compression_alg=file_info.compresson_alg,
        )
        assert result.download_size == file_info.size
        assert result.traffic_on_wire == file_info.size
        assert result.retry_count == 0

    def test_download_zstd_decompresses_payload(
        self, setup_test_data: tuple[Path, list[FileInfo]]
    ) -> None:
        _, file_info_list = setup_test_data
        file_info = next(
            (f for f in file_info_list if f.compresson_alg == "zstd"), None
        )
        if not file_info:
            pytest.skip("no zstd-compressed blob in test corpus")

        dst = self._download_dir / file_info.file_name
        self.downloader.download(
            file_info.url,
            dst,
            digest=file_info.sha256digest,
            size=file_info.size,
            compression_alg=file_info.compresson_alg,
        )
        # post-decompression size on disk equals the metadata size;
        # i.e. the downloader actually unpacked the stream
        assert dst.stat().st_size == file_info.size

    def test_partial_download_when_size_mismatch(
        self, setup_test_data: tuple[Path, list[FileInfo]]
    ) -> None:
        _, file_info_list = setup_test_data
        # uncompressed file so the size we lie about is the on-wire size
        file_info = next(f for f in file_info_list if f.compresson_alg is None)
        with pytest.raises(PartialDownload):
            self.downloader.download(
                file_info.url,
                self._download_dir / file_info.file_name,
                digest=file_info.sha256digest,
                size=file_info.size + 1,  # wrong size
            )

    def test_hash_verification_error_on_digest_mismatch(
        self, setup_test_data: tuple[Path, list[FileInfo]]
    ) -> None:
        _, file_info_list = setup_test_data
        file_info = next(f for f in file_info_list if f.compresson_alg is None)
        with pytest.raises(HashVerificationError):
            self.downloader.download(
                file_info.url,
                self._download_dir / file_info.file_name,
                digest="00" * 32,  # wrong digest
            )

    def test_404_passes_through_as_requests_http_error(
        self,
        setup_test_data: tuple[Path, list[FileInfo]],
        tmp_path: Path,
    ) -> None:
        """``requests``-raised errors are not wrapped — callers can
        distinguish transport errors from downloader-detected ones."""
        _, file_info_list = setup_test_data
        # build a definitely-not-existing URL on the same host
        existing_url = file_info_list[0].url
        base = existing_url.rsplit("/", 1)[0]
        bogus_url = f"{base}/this_file_does_not_exist_xyz"
        with pytest.raises(requests.HTTPError):
            self.downloader.download(
                bogus_url,
                tmp_path / "bogus_out",
                digest="00" * 32,
            )


# ---------- resuming a download against a server that honours Range ---------- #


class _RangeHandler(SimpleHTTPRequestHandler):
    """The corpus served with `Range: bytes=N-` honoured (206 + Content-Range), as a
    CDN or an ota-proxy passing the request through does. A `?start=M` query makes it
    answer the range from M instead of N: a 206 that is not the continuation asked for.
    """

    def do_GET(self):
        path = self.translate_path(self.path)
        _range = self.headers.get("Range")
        _match = re.fullmatch(r"bytes=(\d+)-", _range or "")
        if not _match or not os.path.isfile(path):
            return super().do_GET()
        size = os.path.getsize(path)
        start = int(_match.group(1))
        _query = dict(
            _kv.split("=", 1) for _kv in urlsplit(self.path).query.split("&") if _kv
        )
        if "start" in _query:
            start = int(_query["start"])
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
        self.send_header("Content-Length", str(size - start))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            self.wfile.write(f.read())

    def log_message(self, *args, **kwargs):
        pass


def _serve_corpus_with_ranges(host: str, port: int, directory: str) -> None:
    with ThreadingHTTPServer(
        (host, port), partial(_RangeHandler, directory=directory)
    ) as httpd:
        httpd.serve_forever()


@pytest.fixture(scope="module")
def range_server(setup_test_data: tuple[Path, list[FileInfo]]):
    """The same corpus on a second port, from a server that honours Range."""
    test_data_dir, _ = setup_test_data
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        host, port = s.getsockname()
    server_p = Process(
        target=_serve_corpus_with_ranges,
        args=(host, port, str(test_data_dir)),
        daemon=True,
    )
    server_p.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    try:
        yield f"http://{host}:{port}/"
    finally:
        server_p.terminate()
        server_p.join(timeout=5)


def _on_range_server(file_info: FileInfo, base_url: str, query: str = "") -> str:
    return base_url + file_info.url.rsplit("/", 1)[1] + query


class TestResumedDownloadAgainstLiveServer:
    """`Downloader.download(resume_from=N)` end to end: what goes on the wire, what is
    done with the answer, and what the result describes."""

    @pytest.fixture(autouse=True)
    def setup_downloader(self, tmp_path: Path):
        self.downloader = Downloader(hash_func=sha256, chunk_size=4096)
        self._tmp_path = tmp_path
        yield
        self.downloader.close()

    @staticmethod
    def _uncompressed(file_info_list: list[FileInfo]) -> FileInfo:
        return next(
            f for f in file_info_list if f.compresson_alg is None and f.size > 512
        )

    def _half_of(self, test_data_dir: Path, file_info: FileInfo) -> tuple[Path, int]:
        """The first half of the file, as an interrupted download leaves it."""
        _whole = (test_data_dir / file_info.file_name).read_bytes()
        _dst = self._tmp_path / f"{file_info.file_name}.part"
        _dst.write_bytes(_whole[: len(_whole) // 2])
        return _dst, len(_whole) // 2

    def test_a_server_that_honours_the_range_sends_the_rest_only(
        self, setup_test_data: tuple[Path, list[FileInfo]], range_server: str
    ) -> None:
        test_data_dir, file_info_list = setup_test_data
        file_info = self._uncompressed(file_info_list)
        _dst, _have = self._half_of(test_data_dir, file_info)
        _wire_before = self.downloader.downloaded_bytes

        res = self.downloader.download(
            _on_range_server(file_info, range_server),
            _dst,
            digest=file_info.sha256digest,
            size=file_info.size,
            resume_from=_have,
        )

        assert res.download_size == file_info.size, "the file as it is on disk"
        assert res.traffic_on_wire == file_info.size - _have, "the rest of it only"
        assert self.downloader.downloaded_bytes - _wire_before == file_info.size - _have
        assert _dst.read_bytes() == (test_data_dir / file_info.file_name).read_bytes()

    def test_a_server_that_ignores_the_range_is_downloaded_from_the_start(
        self, setup_test_data: tuple[Path, list[FileInfo]]
    ) -> None:
        """The corpus server (SimpleHTTPRequestHandler) answers 200 to any Range, as
        a server without range support and an ota-proxy that does not pass ranged
        requests through do. The bytes on disk are not built on."""
        test_data_dir, file_info_list = setup_test_data
        file_info = self._uncompressed(file_info_list)
        _dst, _have = self._half_of(test_data_dir, file_info)
        _dst.write_bytes(b"\0" * _have)  # not even the file's bytes

        res = self.downloader.download(
            file_info.url,
            _dst,
            digest=file_info.sha256digest,
            size=file_info.size,
            resume_from=_have,
        )

        assert res.download_size == res.traffic_on_wire == file_info.size
        assert _dst.read_bytes() == (test_data_dir / file_info.file_name).read_bytes()

    def test_a_206_from_somewhere_else_is_downloaded_from_the_start(
        self, setup_test_data: tuple[Path, list[FileInfo]], range_server: str
    ) -> None:
        test_data_dir, file_info_list = setup_test_data
        file_info = self._uncompressed(file_info_list)
        _dst, _have = self._half_of(test_data_dir, file_info)

        res = self.downloader.download(
            _on_range_server(file_info, range_server, "?start=0"),
            _dst,
            digest=file_info.sha256digest,
            size=file_info.size,
            resume_from=_have,
        )

        assert res.traffic_on_wire == file_info.size
        assert _dst.read_bytes() == (test_data_dir / file_info.file_name).read_bytes()

    def test_a_part_of_the_wrong_bytes_is_not_built_on(
        self, setup_test_data: tuple[Path, list[FileInfo]], range_server: str
    ) -> None:
        """The resumed part is trusted no further than a fresh one: the digest over
        the whole file fails on it, and the cache retry fetches the file from the
        start (`retry_on_digest_mismatch` resets resume_from after a hash mismatch)."""
        test_data_dir, file_info_list = setup_test_data
        file_info = self._uncompressed(file_info_list)
        _dst, _have = self._half_of(test_data_dir, file_info)
        _dst.write_bytes(b"\0" * _have)

        res = self.downloader.download(
            _on_range_server(file_info, range_server),
            _dst,
            digest=file_info.sha256digest,
            size=file_info.size,
            resume_from=_have,
        )

        assert res.traffic_on_wire == file_info.size, "the whole file, on the retry"
        assert _dst.read_bytes() == (test_data_dir / file_info.file_name).read_bytes()

    def test_a_request_the_server_refuses_leaves_the_part_as_it_was(
        self, setup_test_data: tuple[Path, list[FileInfo]], range_server: str
    ) -> None:
        """The destination is opened only once the request succeeded: a 404 (or a
        link that drops before the answer) does not truncate what the retry continues."""
        test_data_dir, file_info_list = setup_test_data
        file_info = self._uncompressed(file_info_list)
        _dst, _have = self._half_of(test_data_dir, file_info)
        _before = _dst.read_bytes()

        with pytest.raises(requests.HTTPError):
            self.downloader.download(
                range_server + "this_file_does_not_exist_xyz",
                _dst,
                digest=file_info.sha256digest,
                size=file_info.size,
                resume_from=_have,
            )

        assert _dst.read_bytes() == _before

    def test_a_download_decompressed_as_it_arrives_is_fetched_whole(
        self, setup_test_data: tuple[Path, list[FileInfo]], range_server: str
    ) -> None:
        """There is no byte offset in a decompressed stream to continue from, so the
        range is not asked for at all: the whole blob comes over the wire."""
        test_data_dir, file_info_list = setup_test_data
        file_info = next(f for f in file_info_list if f.compresson_alg == "zstd")
        _on_wire = (test_data_dir / file_info.file_name).stat().st_size
        _dst = self._tmp_path / "decompressed"
        _dst.write_bytes(b"\0" * (file_info.size // 2))

        res = self.downloader.download(
            _on_range_server(file_info, range_server),
            _dst,
            digest=file_info.sha256digest,
            size=file_info.size,
            compression_alg=file_info.compresson_alg,
            resume_from=file_info.size // 2,
        )

        assert res.traffic_on_wire == _on_wire
        assert res.download_size == file_info.size
        assert _dst.stat().st_size == file_info.size


# ---------- header injection on retry, observed end-to-end ---------- #


class TestRetryHeaderInjection:
    """The downloader's ``retry_on_digest_mismatch`` decorator should issue
    a second GET with the ``retry_caching`` directive when the body fails
    verification.  We verify this with the real HTTP server in the loop:
    the first request is real, then a wrap on ``session.get`` raises a
    sentinel exception so we can capture the second call."""

    @pytest.fixture(autouse=True)
    def setup_downloader(self, tmp_path: Path):
        self.downloader = Downloader(hash_func=sha256, chunk_size=4096)
        self._tmp_path = tmp_path
        yield
        self.downloader.close()

    @pytest.mark.parametrize(
        "raised",
        [
            pytest.param(HashVerificationError, id="hash_verification_error"),
            pytest.param(BrokenDecompressionError, id="broken_decompression_error"),
            pytest.param(PartialDownload, id="partial_download"),
        ],
    )
    def test_retry_caching_header_on_verification_failure(
        self,
        raised: type[Exception],
        mocker: pytest_mock.MockerFixture,
        setup_test_data: tuple[Path, list[FileInfo]],
    ) -> None:
        # Replace session.get with a wrapper that always raises the
        # parametrized exception.  We just need to inspect the kwargs of
        # the calls — the downloader code path will not execute.
        mock_get = mocker.MagicMock(
            wraps=self.downloader._session.get, side_effect=raised
        )
        self.downloader._session.get = mock_get

        _, file_info_list = setup_test_data
        file_info = file_info_list[0]

        with pytest.raises(raised):
            self.downloader.download(
                file_info.url,
                self._tmp_path / "out",
                digest="not_important",
            )

        # exactly two GETs: first without retry header, then with
        assert mock_get.call_count == 2
        # first attempt has no cache-control header (no proxy configured)
        first = mock_get.call_args_list[0]
        assert first.kwargs["headers"] is None
        # second attempt has retry_caching directive
        second = mock_get.call_args_list[1]
        retry_headers = second.kwargs["headers"]
        assert retry_headers is not None
        assert "retry_caching" in retry_headers["ota-file-cache-control"]


# ---------- DownloaderPool with ThreadPoolExecutorWithRetry ---------- #


INSTANCE_NUM = 6
MAX_CONCURRENT = 256


class TestDownloaderPoolEndToEnd:
    """Drive the full ``DownloaderPool`` + ``ThreadPoolExecutorWithRetry``
    path used by the production download helpers, against the live HTTP
    server."""

    @pytest.fixture(autouse=True)
    def setup_pool(self, tmp_path: Path):
        self._thread_num = INSTANCE_NUM
        self._downloader_pool = DownloaderPool(
            instance_num=INSTANCE_NUM, hash_func=sha256
        )
        self._downloader_mapper: dict[int, Downloader] = {}
        self._download_dir = tmp_path / "test_download_dir"
        self._download_dir.mkdir(exist_ok=True, parents=True)
        yield
        self._downloader_pool.shutdown()

    def _thread_initializer(self) -> None:
        self._downloader_mapper[threading.get_native_id()] = (
            self._downloader_pool.get_instance()
        )

    def _download_one(self, file_info: FileInfo) -> None:
        downloader = self._downloader_mapper[threading.get_native_id()]
        downloader.download(
            file_info.url,
            self._download_dir / file_info.file_name,
            digest=file_info.sha256digest,
            size=file_info.size,
            compression_alg=file_info.compresson_alg,
        )

    def test_concurrent_download_full_corpus(
        self, setup_test_data: tuple[Path, list[FileInfo]]
    ) -> None:
        _, file_info_list = setup_test_data

        with ThreadPoolExecutorWithRetry(
            max_concurrent=MAX_CONCURRENT,
            max_workers=self._thread_num,
            thread_name_prefix="download_ota_files",
            initializer=self._thread_initializer,
            watchdog_func=partial(
                self._downloader_pool.downloading_watchdog,
                ctx=DownloadPoolWatchdogFuncContext(
                    downloaded_bytes=0,
                    previous_active_timestamp=int(time.time()),
                ),
                max_idle_timeout=6,
            ),
        ) as _mapper:
            for _fut in _mapper.ensure_tasks(self._download_one, file_info_list):
                _fut.result()

        assert self._downloader_pool.total_downloaded_bytes > 0


# ---------- urllib3 / HTTPResponse compatibility, exercised live ---------- #


class TestUrllib3LiveCompatibility:
    """End-to-end smoke checks for the urllib3 APIs we read off
    ``response.raw``.  These complement the construction-time checks in
    the unit suite."""

    @pytest.fixture(autouse=True)
    def setup_downloader(self, tmp_path: Path):
        self.downloader = Downloader(hash_func=sha256, chunk_size=4096)
        self._download_dir = tmp_path / "test_download_dir"
        self._download_dir.mkdir(exist_ok=True, parents=True)
        yield
        self.downloader.close()

    def test_response_raw_is_http_response(
        self, setup_test_data: tuple[Path, list[FileInfo]]
    ) -> None:
        from urllib3.response import HTTPResponse

        _, file_info_list = setup_test_data
        file_info = file_info_list[0]
        with self.downloader._session.get(
            file_info.url, stream=True, timeout=(10, 10)
        ) as resp:
            resp.raise_for_status()
            assert isinstance(resp.raw, HTTPResponse)

    def test_response_raw_retries_history_is_a_tuple(
        self, setup_test_data: tuple[Path, list[FileInfo]]
    ) -> None:
        from urllib3.response import HTTPResponse
        from urllib3.util.retry import Retry

        _, file_info_list = setup_test_data
        file_info = file_info_list[0]
        with self.downloader._session.get(
            file_info.url, stream=True, timeout=(10, 10)
        ) as resp:
            resp.raise_for_status()
            raw_resp: HTTPResponse = resp.raw
            if raw_resp.retries:
                assert isinstance(raw_resp.retries, Retry)
                assert isinstance(raw_resp.retries.history, tuple)

    def test_response_raw_tell_advances(
        self, setup_test_data: tuple[Path, list[FileInfo]]
    ) -> None:
        from urllib3.response import HTTPResponse

        _, file_info_list = setup_test_data
        file_info = next(
            (f for f in file_info_list if f.compresson_alg == "zstd"), None
        )
        if not file_info:
            pytest.skip("no zstd-compressed blob in test corpus")
        with self.downloader._session.get(
            file_info.url, stream=True, timeout=(10, 10)
        ) as resp:
            resp.raise_for_status()
            raw_resp: HTTPResponse = resp.raw
            assert raw_resp.tell() == 0
            chunk = raw_resp.read(100)
            if chunk:
                assert raw_resp.tell() > 0


# ---------- assertions about default config that callers depend on ---------- #


def test_default_retry_status_includes_common_5xx() -> None:
    """``DEFAULT_RETRY_STATUS`` is depended on by ota_core / ota_metadata
    callers — guard the contract so an accidental edit does not silently
    drop retries on common transient codes."""
    for code in (500, 502, 503, 504):
        assert code in DEFAULT_RETRY_STATUS


def test_default_retry_count_is_positive() -> None:
    assert DEFAULT_RETRY_COUNT >= 1
