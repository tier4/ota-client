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
"""Unit tests for otaclient_common.downloader.

These tests exercise the pure-logic pieces of the downloader module:
header injection, cache-policy parsing, decompression adapter behaviour,
the ``retry_on_digest_mismatch`` decorator, and ``DownloaderPool``
thread/instance accounting.  Anything that touches a real HTTP server,
real threads, or real disk I/O lives in the integration tier.
"""

from __future__ import annotations

import io
import threading
from hashlib import sha256

import pytest
import pytest_mock
import zstandard
from requests.structures import CaseInsensitiveDict as CIDict

from otaclient_common.downloader import (
    DEFAULT_RETRY_COUNT,
    DEFAULT_RETRY_STATUS,
    BrokenDecompressionError,
    Downloader,
    DownloaderPool,
    DownloadError,
    DownloadPoolWatchdogFuncContext,
    DownloadResult,
    HashVerificationError,
    PartialDownload,
    ZstdDecompressionAdapter,
    check_cache_policy_in_resp,
    inject_cache_control_header_in_req,
    inject_cache_retry_directory,
    retry_on_digest_mismatch,
)

CACHE_HEADER = "ota-file-cache-control"


# ---------------- check_cache_policy_in_resp ---------------- #


@pytest.mark.parametrize(
    "_input, _resp_headers, _expected",
    [
        pytest.param(
            ("zstd", "matched_digest"),
            CIDict(
                {
                    "Ota-File-Cache-Control": "file_compression_alg=zstd,file_sha256=matched_digest"
                }
            ),
            ("zstd", "matched_digest"),
            id="header_matches_image_meta",
        ),
        pytest.param(
            ("zstd", "image_meta_digest"),
            CIDict(
                {
                    "Ota-File-Cache-Control": "file_compression_alg=zstd,file_sha256=mismatched_digest"
                }
            ),
            HashVerificationError,
            id="digest_mismatch_raises",
        ),
        pytest.param(
            ("mismatched_compression_alg", "matched_digest"),
            CIDict(
                {
                    "Ota-File-Cache-Control": "file_compression_alg=zstd,file_sha256=matched_digest"
                }
            ),
            ("zstd", "matched_digest"),
            id="header_overrides_compression_alg",
        ),
        pytest.param(
            (None, "matched_digest"),
            CIDict(
                {
                    "Ota-File-Cache-Control": "file_compression_alg=zstd,file_sha256=matched_digest"
                }
            ),
            ("zstd", "matched_digest"),
            id="header_supplies_compression_alg_when_input_none",
        ),
        pytest.param(
            ("zstd", "input_digest"),
            CIDict(),
            ("zstd", "input_digest"),
            id="no_header_passthrough",
        ),
        pytest.param(
            (None, None),
            CIDict({"Ota-File-Cache-Control": "file_sha256=not_used"}),
            (None, None),
            id="no_image_meta_digest_ignored",
        ),
    ],
)
def test_check_cache_policy_in_resp(
    _input: tuple[str | None, str | None],
    _resp_headers: CIDict,
    _expected: tuple[str | None, str | None] | type[Exception],
) -> None:
    URL = "dummy_url"
    compression_alg, digest = _input

    if isinstance(_expected, type) and issubclass(_expected, Exception):
        with pytest.raises(_expected):
            check_cache_policy_in_resp(
                URL,
                compression_alg=compression_alg,
                digest=digest,
                resp_headers=_resp_headers,
            )
    else:
        digest, compression_alg = check_cache_policy_in_resp(
            URL,
            compression_alg=compression_alg,
            digest=digest,
            resp_headers=_resp_headers,
        )
        assert (compression_alg, digest) == _expected


# ---------------- inject_cache_control_header_in_req ---------------- #


class TestInjectCacheControlHeaderInReq:
    def test_injects_digest_and_compression_alg(self) -> None:
        headers = inject_cache_control_header_in_req(
            digest="abc123", compression_alg="zstd"
        )
        assert headers is not None
        cache_dir = headers[CACHE_HEADER]
        assert "file_sha256=abc123" in cache_dir
        assert "file_compression_alg=zstd" in cache_dir

    def test_omits_compression_alg_when_none(self) -> None:
        headers = inject_cache_control_header_in_req(
            digest="abc123", compression_alg=None
        )
        assert headers is not None
        cache_dir = headers[CACHE_HEADER]
        assert "file_sha256=abc123" in cache_dir
        assert "file_compression_alg" not in cache_dir

    def test_preserves_existing_headers(self) -> None:
        headers = inject_cache_control_header_in_req(
            digest="abc123",
            input_header={"X-Custom": "v"},
            compression_alg="zstd",
        )
        assert headers is not None
        assert headers["X-Custom"] == "v"
        assert "file_sha256=abc123" in headers[CACHE_HEADER]

    def test_existing_cache_control_is_extended(self) -> None:
        # if the caller already set a cache-control directive, the new
        # digest/compression_alg directives should be merged on top of it
        headers = inject_cache_control_header_in_req(
            digest="abc123",
            input_header={CACHE_HEADER: "no_cache"},
            compression_alg="zstd",
        )
        assert headers is not None
        cache_dir = headers[CACHE_HEADER]
        assert "no_cache" in cache_dir
        assert "file_sha256=abc123" in cache_dir
        assert "file_compression_alg=zstd" in cache_dir


# ---------------- inject_cache_retry_directory ---------------- #


class TestInjectCacheRetryDirectory:
    def test_adds_retry_caching_when_no_headers(self) -> None:
        kwargs: dict = {}
        out = inject_cache_retry_directory(kwargs)
        assert "retry_caching" in out["headers"][CACHE_HEADER]

    def test_preserves_other_headers(self) -> None:
        kwargs: dict = {"headers": {"X-Custom": "v"}}
        out = inject_cache_retry_directory(kwargs)
        assert out["headers"]["X-Custom"] == "v"
        assert "retry_caching" in out["headers"][CACHE_HEADER]

    def test_preserves_existing_cache_policy(self) -> None:
        kwargs: dict = {"headers": {CACHE_HEADER: "file_sha256=abc"}}
        out = inject_cache_retry_directory(kwargs)
        cache_dir = out["headers"][CACHE_HEADER]
        assert "retry_caching" in cache_dir
        assert "file_sha256=abc" in cache_dir

    def test_does_not_mutate_input_kwargs_view(self) -> None:
        # The function pops "headers" out of the dict but should leave a
        # well-formed kwargs back; subsequent calls with the same dict
        # should still produce a retry-caching header.
        kwargs: dict = {"headers": None, "stream": True}
        out = inject_cache_retry_directory(kwargs)
        assert out["stream"] is True
        assert "retry_caching" in out["headers"][CACHE_HEADER]


# ---------------- ZstdDecompressionAdapter ---------------- #


class TestZstdDecompressionAdapter:
    def test_roundtrip_decompression(self) -> None:
        payload = b"x" * 4096
        compressed = zstandard.ZstdCompressor().compress(payload)
        adapter = ZstdDecompressionAdapter()
        result = b"".join(adapter.iter_chunk(io.BytesIO(compressed)))
        assert result == payload

    def test_invalid_stream_raises_broken_decompression(self) -> None:
        adapter = ZstdDecompressionAdapter()
        # plain bytes, not zstd-compressed
        with pytest.raises(BrokenDecompressionError):
            list(adapter.iter_chunk(io.BytesIO(b"not really compressed")))


# ---------------- Downloader.__init__ / config ---------------- #


class TestDownloaderInit:
    def test_default_config_no_proxy(self) -> None:
        d = Downloader(hash_func=sha256, chunk_size=4096)
        try:
            assert d._proxies == {}
            assert d._force_http is False
            assert d.chunk_size == 4096
            assert d.downloaded_bytes == 0
        finally:
            d.close()

    def test_force_http_when_http_proxy_set(self) -> None:
        d = Downloader(
            hash_func=sha256,
            chunk_size=4096,
            proxies={"http": "http://127.0.0.1:8082"},
        )
        try:
            assert d._force_http is True
            assert d._proxies == {"http": "http://127.0.0.1:8082"}
        finally:
            d.close()

    def test_force_http_disabled_when_flag_false(self) -> None:
        d = Downloader(
            hash_func=sha256,
            chunk_size=4096,
            proxies={"http": "http://127.0.0.1:8082"},
            use_http_if_http_proxy_set=False,
        )
        try:
            assert d._force_http is False
        finally:
            d.close()

    def test_https_only_proxy_does_not_force_http(self) -> None:
        d = Downloader(
            hash_func=sha256,
            chunk_size=4096,
            proxies={"https": "http://127.0.0.1:8082"},
        )
        try:
            assert d._force_http is False
        finally:
            d.close()

    def test_cookies_attached_to_session(self) -> None:
        d = Downloader(
            hash_func=sha256,
            chunk_size=4096,
            cookies={"sid": "secret"},
        )
        try:
            assert d._session.cookies.get("sid") == "secret"
        finally:
            d.close()

    def test_get_decompressor_known_alg(self) -> None:
        d = Downloader(hash_func=sha256, chunk_size=4096)
        try:
            assert d._get_decompressor("zstd") is not None
            assert d._get_decompressor("zst") is not None
            # both alias to the same instance
            assert d._get_decompressor("zstd") is d._get_decompressor("zst")
        finally:
            d.close()

    def test_get_decompressor_unknown_alg(self) -> None:
        d = Downloader(hash_func=sha256, chunk_size=4096)
        try:
            assert d._get_decompressor("gzip") is None
            assert d._get_decompressor(None) is None  # type: ignore[arg-type]
        finally:
            d.close()

    def test_close_is_idempotent(self) -> None:
        d = Downloader(hash_func=sha256, chunk_size=4096)
        d.close()
        # calling close a second time must not raise
        d.close()


# ---------------- urllib3 / requests compatibility ---------------- #


class TestUrllib3Compatibility:
    """Smoke tests that fail loudly if urllib3 / requests upgrades drop APIs
    we rely on at construction time."""

    def test_retry_object_creation(self) -> None:
        from urllib3.util.retry import Retry

        retry_config = Retry(
            total=DEFAULT_RETRY_COUNT,
            status_forcelist=DEFAULT_RETRY_STATUS,
            allowed_methods=["GET"],
        )
        assert retry_config.total == DEFAULT_RETRY_COUNT
        assert set(retry_config.status_forcelist or []) == set(DEFAULT_RETRY_STATUS)
        assert retry_config.allowed_methods is not None

    def test_retry_object_integrates_with_http_adapter(self) -> None:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        retry_config = Retry(
            total=5,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=10,
            max_retries=retry_config,
        )
        assert adapter.max_retries.total == 5
        assert set(adapter.max_retries.status_forcelist) == {500, 502, 503, 504}

    def test_session_retry_adapters_configured(self) -> None:
        from urllib3.util.retry import Retry

        d = Downloader(hash_func=sha256, chunk_size=4096)
        try:
            for scheme in ("http://", "https://"):
                adapter = d._session.get_adapter(scheme)
                assert isinstance(adapter.max_retries, Retry)
                assert adapter.max_retries.total == DEFAULT_RETRY_COUNT
                assert set(adapter.max_retries.status_forcelist) == set(
                    DEFAULT_RETRY_STATUS
                )
        finally:
            d.close()


# ---------------- retry_on_digest_mismatch decorator ---------------- #


class TestRetryOnDigestMismatch:
    @pytest.mark.parametrize(
        "raise_exc",
        [
            pytest.param(HashVerificationError, id="hash_verification_error"),
            pytest.param(BrokenDecompressionError, id="broken_decompression_error"),
            pytest.param(PartialDownload, id="partial_download"),
        ],
    )
    def test_retries_once_with_retry_caching_header(
        self, raise_exc: type[Exception]
    ) -> None:
        calls: list[dict] = []

        @retry_on_digest_mismatch
        def _fake_download(*args, **kwargs):
            calls.append(dict(kwargs))
            raise raise_exc("boom")

        with pytest.raises(raise_exc):
            _fake_download(headers=None)

        assert len(calls) == 2, "decorator must call wrapped fn exactly twice"
        # first call passes headers through unchanged
        assert calls[0] == {"headers": None}
        # second call has retry_caching directive injected
        retry_headers = calls[1]["headers"]
        assert retry_headers is not None
        assert "retry_caching" in retry_headers[CACHE_HEADER]

    def test_does_not_retry_on_unrelated_exception(self) -> None:
        calls: list[dict] = []

        class _UnrelatedError(Exception):
            pass

        @retry_on_digest_mismatch
        def _fake_download(*args, **kwargs):
            calls.append(dict(kwargs))
            raise _UnrelatedError("nope")

        with pytest.raises(_UnrelatedError):
            _fake_download()

        assert len(calls) == 1

    def test_does_not_retry_on_success(self) -> None:
        calls: list[dict] = []

        @retry_on_digest_mismatch
        def _fake_download(*args, **kwargs):
            calls.append(dict(kwargs))
            return DownloadResult(0, 100, 100)

        out = _fake_download()
        assert isinstance(out, DownloadResult)
        assert len(calls) == 1


# ---------------- Downloader.download header injection ---------------- #


class TestDownloadHeaderInjection:
    """Verify ota-file-cache-control header injection without doing real HTTP.

    The session.get is replaced with a mock that raises a sentinel exception
    so we can inspect the request kwargs without entering the response/IO
    handling path.
    """

    @pytest.fixture
    def downloader(self):
        d = Downloader(hash_func=sha256, chunk_size=4096)
        yield d
        d.close()

    def test_inject_cache_control_when_proxy_and_digest_set(
        self,
        downloader: Downloader,
        mocker: pytest_mock.MockerFixture,
        tmp_path,
    ) -> None:
        class _Sentinel(Exception):
            pass

        mock_get = mocker.MagicMock(side_effect=_Sentinel)
        downloader._session.get = mock_get
        mocker.patch.object(
            downloader, "_proxies", new={"http": "http://127.0.0.1:8082"}
        )

        with pytest.raises(_Sentinel):
            downloader.download(
                "http://127.0.0.1/some_file",
                tmp_path / "out",
                digest="aabbcc",
                compression_alg="zstd",
            )

        # find the call that was actually made (decorator may retry on certain
        # exceptions, but _Sentinel is not in the retry set so there must be
        # exactly one)
        assert mock_get.call_count == 1
        kwargs = mock_get.call_args.kwargs
        cache_dir = kwargs["headers"][CACHE_HEADER]
        assert "file_sha256=aabbcc" in cache_dir
        assert "file_compression_alg=zstd" in cache_dir
        # streaming GET with timeout
        assert kwargs["stream"] is True
        assert kwargs["timeout"] is not None

    def test_no_header_injection_without_proxy(
        self,
        downloader: Downloader,
        mocker: pytest_mock.MockerFixture,
        tmp_path,
    ) -> None:
        class _Sentinel(Exception):
            pass

        mock_get = mocker.MagicMock(side_effect=_Sentinel)
        downloader._session.get = mock_get

        with pytest.raises(_Sentinel):
            downloader.download(
                "http://127.0.0.1/some_file",
                tmp_path / "out",
                digest="aabbcc",
                compression_alg="zstd",
            )

        # no proxy → no cache-control header should be injected
        assert mock_get.call_count == 1
        assert mock_get.call_args.kwargs["headers"] is None

    def test_force_http_rewrites_https_url(
        self,
        downloader: Downloader,
        mocker: pytest_mock.MockerFixture,
        tmp_path,
    ) -> None:
        class _Sentinel(Exception):
            pass

        mock_get = mocker.MagicMock(side_effect=_Sentinel)
        downloader._session.get = mock_get
        mocker.patch.object(downloader, "_force_http", new=True)

        with pytest.raises(_Sentinel):
            downloader.download(
                "https://example.com/file",
                tmp_path / "out",
                digest="aabbcc",
            )

        # url scheme rewritten from https → http
        assert mock_get.call_args.args[0].startswith("http://")
        assert "https://" not in mock_get.call_args.args[0]


# ---------------- DownloaderPool ---------------- #


class TestDownloaderPool:
    def test_get_instance_returns_same_inst_for_same_thread(self) -> None:
        pool = DownloaderPool(instance_num=2, hash_func=sha256)
        try:
            d1 = pool.get_instance()
            d2 = pool.get_instance()
            assert d1 is d2
        finally:
            pool.shutdown()

    def test_distinct_threads_get_distinct_instances(self) -> None:
        pool = DownloaderPool(instance_num=2, hash_func=sha256)
        try:
            received: dict[int, Downloader] = {}
            barrier = threading.Barrier(2)

            def _worker():
                # synchronize so both threads claim their slot before either
                # releases (otherwise the second thread could reuse slot 0)
                barrier.wait()
                received[threading.get_ident()] = pool.get_instance()
                barrier.wait()

            t1 = threading.Thread(target=_worker)
            t2 = threading.Thread(target=_worker)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            assert len(received) == 2
            instances = list(received.values())
            assert instances[0] is not instances[1]
        finally:
            pool.shutdown()

    def test_pool_exhaustion_raises(self) -> None:
        pool = DownloaderPool(instance_num=1, hash_func=sha256)
        try:
            # main thread takes the only slot
            pool.get_instance()
            err: list[BaseException] = []

            def _other():
                try:
                    pool.get_instance()
                except BaseException as e:
                    err.append(e)

            t = threading.Thread(target=_other)
            t.start()
            t.join()
            assert len(err) == 1 and isinstance(err[0], ValueError)
        finally:
            pool.shutdown()

    def test_release_instance_makes_slot_available(self) -> None:
        pool = DownloaderPool(instance_num=1, hash_func=sha256)
        try:
            d_main = pool.get_instance()
            pool.release_instance()
            # another thread can now claim
            received: list[Downloader] = []

            def _other():
                received.append(pool.get_instance())

            t = threading.Thread(target=_other)
            t.start()
            t.join()
            assert len(received) == 1
            # in a single-slot pool, the only instance is reused
            assert received[0] is d_main
        finally:
            pool.shutdown()

    def test_release_all_instances_clears_mapping(self) -> None:
        pool = DownloaderPool(instance_num=2, hash_func=sha256)
        try:
            pool.get_instance()
            assert len(pool._thread_idx_mapping) == 1
            pool.release_all_instances()
            assert pool._thread_idx_mapping == {}
            assert all(
                idx == DownloaderPool.INSTANCE_AVAILABLE_ID
                for idx in pool._idx_thread_mapping
            )
        finally:
            pool.shutdown()

    def test_shutdown_closes_all_downloaders(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        pool = DownloaderPool(instance_num=3, hash_func=sha256)
        spies = [mocker.spy(d, "close") for d in pool._instances]
        pool.shutdown()
        for spy in spies:
            spy.assert_called_once()
        assert pool._instances == []

    def test_total_downloaded_bytes_aggregates(self) -> None:
        pool = DownloaderPool(instance_num=2, hash_func=sha256)
        try:
            pool._instances[0]._downloaded_bytes = 100
            pool._instances[1]._downloaded_bytes = 200
            assert pool.total_downloaded_bytes == 300
        finally:
            pool.shutdown()

    def test_total_downloaded_bytes_after_shutdown_uses_cached_value(self) -> None:
        pool = DownloaderPool(instance_num=2, hash_func=sha256)
        pool._instances[0]._downloaded_bytes = 100
        pool._instances[1]._downloaded_bytes = 200
        pool.shutdown()
        # after shutdown, instances list is cleared but the cached aggregate
        # is preserved so the caller can still report stats
        assert pool.total_downloaded_bytes == 300


# ---------------- DownloaderPool.downloading_watchdog ---------------- #


class TestDownloadingWatchdog:
    """Drive the watchdog with a fake clock so the test does not actually
    sleep through the timeout window."""

    def test_progress_resets_idle_timer(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        pool = DownloaderPool(instance_num=1, hash_func=sha256)
        try:
            ctx = DownloadPoolWatchdogFuncContext(
                downloaded_bytes=0,
                previous_active_timestamp=1000,
            )
            mocker.patch("otaclient_common.downloader.time.time", return_value=1010)
            # simulate progress
            pool._instances[0]._downloaded_bytes = 50
            pool.downloading_watchdog(ctx=ctx, max_idle_timeout=5)
            assert ctx["downloaded_bytes"] == 50
            assert ctx["previous_active_timestamp"] == 1010
        finally:
            pool.shutdown()

    def test_no_progress_within_timeout_does_not_raise(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        pool = DownloaderPool(instance_num=1, hash_func=sha256)
        try:
            ctx = DownloadPoolWatchdogFuncContext(
                downloaded_bytes=0,
                previous_active_timestamp=1000,
            )
            # 4s elapsed, well under the 10s timeout
            mocker.patch("otaclient_common.downloader.time.time", return_value=1004)
            # downloader has not made progress
            pool.downloading_watchdog(ctx=ctx, max_idle_timeout=10)
        finally:
            pool.shutdown()

    def test_no_progress_past_timeout_raises_value_error(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        pool = DownloaderPool(instance_num=1, hash_func=sha256)
        try:
            ctx = DownloadPoolWatchdogFuncContext(
                downloaded_bytes=0,
                previous_active_timestamp=1000,
            )
            # 30s elapsed, past the 10s timeout
            mocker.patch("otaclient_common.downloader.time.time", return_value=1030)
            with pytest.raises(ValueError):
                pool.downloading_watchdog(ctx=ctx, max_idle_timeout=10)
        finally:
            pool.shutdown()


# ---------------- error type hierarchy ---------------- #


def test_download_error_hierarchy() -> None:
    """Downloader callers catch ``DownloadError`` to distinguish downloader-
    raised conditions from the underlying ``requests`` exceptions; verify
    the hierarchy stays intact."""
    assert issubclass(PartialDownload, DownloadError)
    assert issubclass(HashVerificationError, DownloadError)
    assert issubclass(BrokenDecompressionError, DownloadError)
