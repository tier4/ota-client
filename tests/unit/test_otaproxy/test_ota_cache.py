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

from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import anyio
import pytest
import zstandard
from multidict import CIMultiDict, CIMultiDictProxy

from ota_proxy import config as cfg
from ota_proxy._consts import (
    HEADER_CONTENT_ENCODING,
    HEADER_CONTENT_RANGE,
    HEADER_OTA_FILE_CACHE_CONTROL,
    HEADER_RANGE,
)
from ota_proxy.cache_control_header import export_kwargs_as_header_string
from ota_proxy.cache_index import CacheIndex
from ota_proxy.cache_streaming import (
    CacheTracker,
    CacheTrackerEvents,
    CacheWriterPool,
)
from ota_proxy.db import CacheMeta
from ota_proxy.errors import CacheProviderNotReady
from ota_proxy.ota_cache import OTACache, _range_start
from ota_proxy.utils import url_based_hash
from otaclient_common.downloader import DEFAULT_READ_TIMEOUT

logger = logging.getLogger(__name__)

TEST_DATA_SET_SIZE = 4096
TEST_LOOKUP_ENTRIES = 1200
TEST_DELETE_ENTRIES = 512


def _generate_test_entries() -> dict[str, CacheMeta]:
    """Generate a set of CacheMeta entries with various cache sizes."""
    size_list = [0, 1024, 2048, 4096, 8192, 16384, 32768, 256 * 1024, 1024**2]

    entries: dict[str, CacheMeta] = {}
    for idx in range(TEST_DATA_SET_SIZE):
        target_size = random.choice(size_list)
        mocked_url = f"#{idx}/w/targetsize={target_size}"
        file_sha256 = url_based_hash(mocked_url)

        entries[file_sha256] = CacheMeta(
            url=mocked_url,
            cache_size=target_size,
            file_sha256=file_sha256,
        )
    return entries


#
# ------------ Tests: CacheIndex ------------ #
#


class TestCacheIndex:
    """Tests for CacheIndex commit, lookup, and remove operations."""

    @pytest.fixture(scope="class")
    def test_entries(self) -> dict[str, CacheMeta]:
        return _generate_test_entries()

    @pytest.fixture(scope="class")
    def cache_index(self, tmp_path_factory: pytest.TempPathFactory):
        ota_cache_folder = tmp_path_factory.mktemp("cache_index") / "ota-cache"
        ota_cache_folder.mkdir()
        db_f = ota_cache_folder / "db_f"

        # Create cache files so preload doesn't discard entries
        # (preload checks os.path.exists for each entry)
        cache_index = CacheIndex(db_f, ota_cache_folder, force_init_db=True)
        try:
            yield cache_index, ota_cache_folder
        finally:
            cache_index.close()

    def test_commit_entry(
        self,
        cache_index: tuple[CacheIndex, Path],
        test_entries: dict[str, CacheMeta],
    ):
        idx, base_dir = cache_index
        for entry in test_entries.values():
            # Create a placeholder cache file so the entry is valid on reload
            (base_dir / entry.file_sha256).touch()
            assert idx.commit_entry(entry)

    def test_lookup_entry(
        self,
        cache_index: tuple[CacheIndex, Path],
        test_entries: dict[str, CacheMeta],
    ):
        idx, _ = cache_index
        # Confirm all entries are added
        for entry in test_entries.values():
            result = idx.lookup_entry(entry.file_sha256)
            assert result is not None
            assert result.cache_size == test_entries[entry.file_sha256].cache_size

    def test_remove_entry(
        self,
        cache_index: tuple[CacheIndex, Path],
        test_entries: dict[str, CacheMeta],
    ):
        idx, _ = cache_index

        # Remove a sample and verify they are gone
        entries_to_remove = random.sample(
            list(test_entries.values()), k=TEST_DELETE_ENTRIES
        )
        for entry in entries_to_remove:
            idx.remove_entry(entry.file_sha256)
            assert idx.lookup_entry(entry.file_sha256) is None


class TestCacheIndexEntryHeaders:
    """CacheIndex.commit_entry pre-computes CacheIndexEntry.headers so that
    ota_cache.py can return them directly without rebuilding per request."""

    def test_committed_entry_exposes_precomputed_headers(
        self, tmp_path_factory: pytest.TempPathFactory
    ):
        base_dir = tmp_path_factory.mktemp("cache_index_headers") / "ota-cache"
        base_dir.mkdir()
        db_f = base_dir / "db_f"
        idx = CacheIndex(db_f, base_dir, force_init_db=True)
        try:
            real_sha = "b" * 64
            meta = CacheMeta(
                file_sha256=real_sha,
                url="http://example.com/compressed",
                cache_size=1024,
                file_compression_alg="zst",
                content_encoding="zstd",
            )
            (base_dir / real_sha).touch()
            assert idx.commit_entry(meta)

            looked_up = idx.lookup_entry(real_sha)
            assert looked_up is not None
            assert looked_up.cache_size == 1024
            assert looked_up.headers[HEADER_CONTENT_ENCODING] == "zstd"
            cache_ctrl = looked_up.headers[HEADER_OTA_FILE_CACHE_CONTROL]
            assert f"file_sha256={real_sha}" in cache_ctrl
            assert "file_compression_alg=zst" in cache_ctrl
        finally:
            idx.close()

    def test_url_based_hash_entry_headers_omit_cache_control(
        self, tmp_path_factory: pytest.TempPathFactory
    ):
        """URL-based hash entries do not have a known file sha, so the
        ota-file-cache-control header must not be emitted."""
        base_dir = tmp_path_factory.mktemp("cache_index_url_headers") / "ota-cache"
        base_dir.mkdir()
        db_f = base_dir / "db_f"
        idx = CacheIndex(db_f, base_dir, force_init_db=True)
        try:
            url = "http://example.com/opaque"
            key = url_based_hash(url)
            meta = CacheMeta(file_sha256=key, url=url, cache_size=32)
            (base_dir / key).touch()
            assert idx.commit_entry(meta)

            looked_up = idx.lookup_entry(key)
            assert looked_up is not None
            assert HEADER_OTA_FILE_CACHE_CONTROL not in looked_up.headers
        finally:
            idx.close()


#
# ------------ Tests: OTACache CDN cache-hit detection ------------ #
#


class TestOTACacheCDNCacheHit:
    """Tests for CDN cache-hit detection via X-Cache response header."""

    @pytest.mark.parametrize(
        "x_cache_value, expected_hits",
        [
            ("Hit from CloudFront", 1),
            ("hit from cloudfront", 1),
            ("Miss from CloudFront", 0),
            ("miss from cloudfront", 0),
        ],
    )
    async def test_retrieve_file_by_downloading_cdn_cache(
        self, x_cache_value: str, expected_hits: int
    ):
        cache = OTACache(
            cache_enabled=False,
            init_cache=False,
            shm_metrics_writer=None,
        )

        async def mock_do_request(raw_url, headers):
            yield CIMultiDictProxy(CIMultiDict({"X-Cache": x_cache_value}))
            yield b"data"

        cache._do_request = MagicMock(side_effect=mock_do_request)

        remote_fd, _ = await cache._retrieve_file_by_downloading("dummy_url", {})
        [chunk async for chunk in remote_fd]  # consume the generator
        assert cache._metrics_data.cache_cdn_hits == expected_hits


#
# ------------ Tests: OTACache DNS TTL configuration ------------ #
#


class TestOTACacheDNSTTL:
    """Tests for AIOHTTP_DNS_CACHE_TTL configuration bounds and application."""

    def test_dns_cache_ttl_config_within_reasonable_range(self):
        """AIOHTTP_DNS_CACHE_TTL should be longer than aiohttp default (10s) but
        short enough to allow CDN failover (CloudFront TTL is ~60-300s)."""
        assert cfg.AIOHTTP_DNS_CACHE_TTL > 10, (
            "TTL should be greater than aiohttp default (10s) to reduce DNS resolution frequency"
        )
        assert cfg.AIOHTTP_DNS_CACHE_TTL <= 300, (
            "TTL should not exceed CDN DNS TTL (~300s) to allow IP failover"
        )

    async def test_ota_cache_start_applies_dns_cache_ttl(self):
        """OTACache.start() should configure TCPConnector with AIOHTTP_DNS_CACHE_TTL."""
        cache = OTACache(
            cache_enabled=False,
            init_cache=False,
            shm_metrics_writer=None,
        )
        with patch.object(
            aiohttp, "TCPConnector", wraps=aiohttp.TCPConnector
        ) as mock_connector:
            await cache.start()
            try:
                mock_connector.assert_called_once_with(
                    ttl_dns_cache=cfg.AIOHTTP_DNS_CACHE_TTL
                )
            finally:
                await cache.close()


#
# ------------ Tests: OTACache socket read timeout ------------ #
#

BLACKOUT_SILENCE = 28  # seconds
"""The silence a link blackout of ~15s leaves on a TCP stream.

TCP retransmission backs off during the blackout: the first retransmission after the
    link comes back is ~25-28s after the last data (measured with tc netem, RTT 60ms).
"""


class TestOTACacheSocketReadTimeout:
    def test_otaproxy_gives_up_on_a_stalled_remote_before_its_client_does(self):
        """otaproxy ends its response on a stalled remote before the client times out."""
        assert DEFAULT_READ_TIMEOUT - cfg.AIOHTTP_SOCKET_READ_TIMEOUT >= 2, (
            "otaproxy must give up on the remote before the client gives up on otaproxy"
        )

    def test_otaproxy_rides_through_a_short_link_blackout(self):
        assert cfg.AIOHTTP_SOCKET_READ_TIMEOUT > BLACKOUT_SILENCE, (
            "a link blackout of ~15s must not cut the responses in flight"
        )

    def test_subscribers_wait_as_long_as_the_provider(self):
        """A subscriber of an ongoing caching doesn't cut what the provider rides through."""
        assert (
            cfg.AIOHTTP_SOCKET_READ_TIMEOUT
            <= CacheTracker.SUBSCRIBER_WAIT_TIMEOUT
            < DEFAULT_READ_TIMEOUT
        ), "a subscriber must wait as long as the provider, and less than the client"

    async def test_ota_cache_start_applies_socket_read_timeout(self):
        cache = OTACache(
            cache_enabled=False,
            init_cache=False,
            shm_metrics_writer=None,
        )
        with patch.object(
            aiohttp, "ClientTimeout", wraps=aiohttp.ClientTimeout
        ) as mock_timeout:
            await cache.start()
            try:
                mock_timeout.assert_called_once_with(
                    total=None, sock_read=cfg.AIOHTTP_SOCKET_READ_TIMEOUT
                )
            finally:
                await cache.close()


#
# ------------ Tests: a provider whose cache writer gets no thread ------------ #
#


class TestOTACacheWriterGetsNoThread:
    """A cache writer keeps its thread until its provider has received the whole file, so
    slow downloads can hold all the write threads. The writer of a new provider then waits
    in the queue, and its subscribers cannot start until a thread frees up."""

    def test_subscribers_are_released_before_they_give_up(self):
        assert cfg.CACHE_WRITER_START_TIMEOUT < CacheTracker.SUBSCRIBER_WAIT_TIMEOUT, (
            "a writer that gets no thread must be dropped before its subscribers give up"
        )

    async def test_the_subscriber_fetches_the_file_by_itself(
        self, tmp_path_factory: pytest.TempPathFactory
    ):
        """The subscriber is answered 503 as soon as the writer is dropped, not after
        the slow downloads holding the threads finish, and its next request downloads
        the file from the remote as a provider."""
        cache = OTACache(
            cache_enabled=True,
            init_cache=False,
            base_dir=tmp_path_factory.mktemp("ota-cache") / "local_cache",
        )
        _url, _sha = "http://example.com/blob", "c" * 64
        _chunk, _chunks_num = b"x" * 1024, 20

        def _request() -> CIMultiDict[str]:
            return CIMultiDict({HEADER_OTA_FILE_CACHE_CONTROL: f"file_sha256={_sha}"})

        async def _slow_remote():
            for _ in range(_chunks_num):
                yield _chunk
                await asyncio.sleep(0.02)

        ongoing_writer_done = threading.Event()
        with (
            patch(
                "ota_proxy.ota_cache.shutil.disk_usage",
                return_value=MagicMock(total=100, used=10, free=90),
            ),
            patch.object(
                cache, "_retrieve_file_by_downloading", new_callable=AsyncMock
            ) as mock_remote,
        ):
            mock_remote.side_effect = lambda *_, **__: (
                _slow_remote(),
                CIMultiDictProxy(CIMultiDict()),
            )
            await cache.start()
            try:
                assert await asyncio.to_thread(
                    cache._storage_below_hard_limit_event.wait, 5
                )
                await cache._cache_write_pool.close()
                cache._cache_write_pool = CacheWriterPool(
                    max_workers=1, writer_start_timeout=0.2
                )
                # the only write thread is held by an ongoing slow download
                await cache._cache_write_pool._write_dispatcher(
                    CacheTrackerEvents(), ongoing_writer_done.wait
                )

                provider_body, _ = await cache.retrieve_file(_url, _request())
                provider = asyncio.create_task(_collect(provider_body))
                await asyncio.sleep(0.1)  # the provider's writer is queued by now

                _t0 = time.monotonic()
                with pytest.raises(CacheProviderNotReady):
                    await cache.retrieve_file(_url, _request())
                assert time.monotonic() - _t0 < CacheTracker.SUBSCRIBER_WAIT_TIMEOUT / 2

                retry_body, _ = await cache.retrieve_file(_url, _request())
                assert mock_remote.call_count == 2, "the retry must go to the remote"
                assert await _collect(retry_body) == _chunk * _chunks_num
                assert await provider == _chunk * _chunks_num
            finally:
                ongoing_writer_done.set()
                await cache.close()


#
# ------------ Tests: OTACache NFS cache lookup ordering ------------ #
#


class TestOTACacheRangedRequest:
    """A ranged request is a client continuing an interrupted download."""

    async def test_it_goes_to_the_remote_and_is_not_cached(
        self, tmp_path_factory: pytest.TempPathFactory
    ):
        """The cache keeps whole files: the rest of one is neither looked up in it nor
        cached, and the Range header reaches the remote."""
        cache = OTACache(
            cache_enabled=True,
            init_cache=False,
            base_dir=tmp_path_factory.mktemp("ota-cache") / "local_cache",
        )
        remote_result = (
            AsyncMock(),
            CIMultiDict({HEADER_CONTENT_RANGE: "bytes 100-199/200"}),
        )

        with (
            patch.object(
                cache, "_retrieve_file_by_cache_lookup", new_callable=AsyncMock
            ) as mock_local,
            patch.object(
                cache, "_retrieve_file_by_new_caching", new_callable=AsyncMock
            ) as mock_caching,
            patch.object(
                cache, "_retrieve_file_by_downloading", new_callable=AsyncMock
            ) as mock_remote,
        ):
            mock_remote.return_value = remote_result

            await cache.start()
            try:
                result = await cache.retrieve_file(
                    "http://example.com/file",
                    CIMultiDict(
                        {
                            HEADER_RANGE: "bytes=100-",
                            HEADER_OTA_FILE_CACHE_CONTROL: f"file_sha256={'a' * 64}",
                        }
                    ),
                )
            finally:
                await cache.close()

        assert result == remote_result
        mock_local.assert_not_called()
        mock_caching.assert_not_called()
        assert mock_remote.call_args.kwargs["headers"][HEADER_RANGE] == "bytes=100-"

    @staticmethod
    async def _ranged_retry(cache: OTACache, *, file_sha256: str):
        """What the client's cache retry of a download cut short sends: the rest of
        the file, and that the cached entry it got is corrupted."""
        return await cache.retrieve_file(
            "http://example.com/file",
            CIMultiDict(
                {
                    HEADER_RANGE: "bytes=100-",
                    HEADER_OTA_FILE_CACHE_CONTROL: f"file_sha256={file_sha256},retry_caching",
                }
            ),
        )

    async def test_an_entry_the_client_found_corrupted_is_dropped_first(
        self, tmp_path_factory: pytest.TempPathFactory
    ):
        """A cached file left short by a crash is what the client got; it reports it
        with retry_caching as it asks for the rest. The entry is dropped before the
        request goes through, so that the next client caches the file whole."""
        base_dir = tmp_path_factory.mktemp("ota-cache") / "local_cache"
        cache = OTACache(cache_enabled=True, init_cache=False, base_dir=base_dir)
        _sha = "a" * 64

        with patch.object(
            cache, "_retrieve_file_by_downloading", new_callable=AsyncMock
        ) as mock_remote:
            mock_remote.return_value = (AsyncMock(), CIMultiDict())
            await cache.start()
            try:
                (base_dir / _sha).write_bytes(b"short")
                cache._cache_index.remove_entry = MagicMock()

                await self._ranged_retry(cache, file_sha256=_sha)

                cache._cache_index.remove_entry.assert_called_once_with(_sha)
                assert not (base_dir / _sha).exists()
            finally:
                await cache.close()
        assert mock_remote.call_args.kwargs["headers"][HEADER_RANGE] == "bytes=100-"

    async def test_an_entry_a_provider_is_caching_anew_is_left_to_it(
        self, tmp_path_factory: pytest.TempPathFactory
    ):
        """The provider dropped the old entry before it started; its file is the fresh
        one and must not be removed under it."""
        base_dir = tmp_path_factory.mktemp("ota-cache") / "local_cache"
        cache = OTACache(cache_enabled=True, init_cache=False, base_dir=base_dir)
        _sha = "b" * 64

        with patch.object(
            cache, "_retrieve_file_by_downloading", new_callable=AsyncMock
        ) as mock_remote:
            mock_remote.return_value = (AsyncMock(), CIMultiDict())
            await cache.start()
            try:
                (base_dir / _sha).write_bytes(b"being cached anew")
                _tracker = MagicMock()
                _tracker._tracker_events.writer_failed = False
                cache._on_going_caching.register_tracker(_sha, _tracker)
                cache._cache_index.remove_entry = MagicMock()

                await self._ranged_retry(cache, file_sha256=_sha)

                cache._cache_index.remove_entry.assert_not_called()
                assert (base_dir / _sha).exists()
            finally:
                await cache.close()


#
# ------------ Tests: a ranged request answered from a whole cached copy ------------ #
#


async def _collect(body) -> bytes:
    """What the client receives: a bytes object, or an async generator of chunks."""
    if isinstance(body, bytes):
        return body
    _chunks = []
    async for _chunk in body:
        _chunks.append(_chunk)
    return b"".join(_chunks)


def _ranged_request(file_sha256: str, start: int, *extra_directives: str):
    _directives = ",".join([f"file_sha256={file_sha256}", *extra_directives])
    return CIMultiDict(
        {
            HEADER_RANGE: f"bytes={start}-",
            HEADER_OTA_FILE_CACHE_CONTROL: _directives,
        }
    )


class TestRangeStart:
    def test_the_rest_of_a_file(self):
        assert _range_start("bytes=1048576-") == 1048576
        assert _range_start(" bytes=0- ") == 0

    @pytest.mark.parametrize(
        "header", ["bytes=0-99", "bytes=-100", "bytes=1-,5-", "items=1-", "", "bytes="]
    )
    def test_any_other_range_is_left_to_the_remote(self, header: str):
        assert _range_start(header) is None


class TestOTACacheRangeFromCache:
    """A client continuing an interrupted download asks for the rest of a file. When a
    cache source holds the file whole, the rest comes from there, not from the remote:
    the cache keeps serving when the remote is gone (an external cache in a workshop),
    and a copy another ECU cached meanwhile is used."""

    @pytest.fixture
    async def cache(self, tmp_path_factory: pytest.TempPathFactory):
        _cache = OTACache(
            cache_enabled=True,
            init_cache=False,
            base_dir=tmp_path_factory.mktemp("ota-cache") / "local_cache",
        )
        _cache._external_cache_data_dir = None
        _cache._external_nfs_cache_data_dir = None
        with patch.object(
            _cache, "_retrieve_file_by_downloading", new_callable=AsyncMock
        ) as mock_remote:
            mock_remote.return_value = (AsyncMock(), CIMultiDict({"from": "remote"}))
            _cache.mock_remote = mock_remote  # type: ignore[attr-defined]
            await _cache.start()
            try:
                yield _cache
            finally:
                await _cache.close()

    @staticmethod
    def cache_whole(cache: OTACache, data: bytes, *, compression_alg=None) -> str:
        """A file cached whole: its entry in the index and its bytes on disk."""
        _sha = os.urandom(32).hex()
        (cache._base_dir_sync / _sha).write_bytes(data)
        assert cache._cache_index.commit_entry(
            CacheMeta(
                file_sha256=_sha,
                url="http://example.com/file",
                cache_size=len(data),
                file_compression_alg=compression_alg,
            )
        )
        return _sha

    @pytest.mark.parametrize(
        "size",
        [
            pytest.param(4096, id="small_file_read_at_once"),
            pytest.param(cfg.REMOTE_READ_CHUNK_SIZE * 2 + 17, id="large_file_streamed"),
        ],
    )
    async def test_the_rest_of_a_locally_cached_file_comes_from_the_cache(
        self, cache: OTACache, size: int
    ):
        _data = os.urandom(size)
        _sha = self.cache_whole(cache, _data)
        _start = size // 3

        body, headers = await cache.retrieve_file(
            "http://example.com/file", _ranged_request(_sha, _start)
        )

        assert await _collect(body) == _data[_start:]
        assert headers[HEADER_CONTENT_RANGE] == f"bytes {_start}-{size - 1}/{size}"
        assert f"file_sha256={_sha}" in headers[HEADER_OTA_FILE_CACHE_CONTROL]
        cache.mock_remote.assert_not_called()  # type: ignore[attr-defined]
        assert cache._metrics_data.cache_local_hits == 1

    async def test_the_entrys_own_headers_travel_with_the_range(self, cache: OTACache):
        """What a whole-file hit would tell the client (the compression it was cached
        with, say), the range tells it too."""
        _data = os.urandom(1000)
        _sha = self.cache_whole(cache, _data, compression_alg="zst")

        _, headers = await cache.retrieve_file(
            "http://example.com/file", _ranged_request(_sha, 10)
        )

        assert "file_compression_alg=zst" in headers[HEADER_OTA_FILE_CACHE_CONTROL]
        assert headers[HEADER_CONTENT_RANGE] == "bytes 10-999/1000"

    async def test_a_file_a_crash_left_short_is_not_built_on(self, cache: OTACache):
        """The index says 1000 bytes, the disk holds 600: the copy is not whole, so the
        rest comes from the remote (and the client's retry_caching will drop it)."""
        _data = os.urandom(1000)
        _sha = self.cache_whole(cache, _data)
        (cache._base_dir_sync / _sha).write_bytes(_data[:600])

        body, headers = await cache.retrieve_file(
            "http://example.com/file", _ranged_request(_sha, 100)
        )

        assert headers["from"] == "remote"
        cache.mock_remote.assert_called_once()  # type: ignore[attr-defined]
        assert cache._metrics_data.cache_local_hits == 0

    @pytest.mark.parametrize("start", [1000, 1001], ids=["at_the_end", "past_it"])
    async def test_a_start_not_inside_the_file_is_left_to_the_remote(
        self, cache: OTACache, start: int
    ):
        _sha = self.cache_whole(cache, os.urandom(1000))

        _, headers = await cache.retrieve_file(
            "http://example.com/file", _ranged_request(_sha, start)
        )

        assert headers["from"] == "remote"

    async def test_a_file_the_cache_does_not_hold_is_left_to_the_remote(
        self, cache: OTACache
    ):
        _, headers = await cache.retrieve_file(
            "http://example.com/file", _ranged_request("a" * 64, 100)
        )

        assert headers["from"] == "remote"
        _headers_to_remote = cache.mock_remote.call_args.kwargs["headers"]  # type: ignore[attr-defined]
        assert _headers_to_remote[HEADER_RANGE] == "bytes=100-"

    @pytest.mark.parametrize("header", ["bytes=0-99", "bytes=-100"])
    async def test_a_range_that_is_not_the_rest_of_the_file_is_left_to_the_remote(
        self, cache: OTACache, header: str
    ):
        _sha = self.cache_whole(cache, os.urandom(1000))

        _, headers = await cache.retrieve_file(
            "http://example.com/file",
            CIMultiDict(
                {
                    HEADER_RANGE: header,
                    HEADER_OTA_FILE_CACHE_CONTROL: f"file_sha256={_sha}",
                }
            ),
        )

        assert headers["from"] == "remote"

    async def test_no_cache_means_no_cache_for_a_range_either(self, cache: OTACache):
        _sha = self.cache_whole(cache, os.urandom(1000))

        _, headers = await cache.retrieve_file(
            "http://example.com/file", _ranged_request(_sha, 100, "no_cache")
        )

        assert headers["from"] == "remote"
        assert cache._metrics_data.cache_local_hits == 0

    async def test_a_copy_the_client_reported_corrupted_is_not_served_but_dropped(
        self, cache: OTACache
    ):
        """retry_caching with the range: the client got this copy short (or wrong),
        so the copy is dropped and the rest comes from the remote."""
        _data = os.urandom(1000)
        _sha = self.cache_whole(cache, _data)

        _, headers = await cache.retrieve_file(
            "http://example.com/file", _ranged_request(_sha, 100, "retry_caching")
        )

        assert headers["from"] == "remote"
        assert not (cache._base_dir_sync / _sha).exists()
        assert cache._cache_index.lookup_entry(_sha) is None

    async def test_a_url_based_entry_serves_its_range_too(self, cache: OTACache):
        """A file cached without a known digest (a metafile) is keyed by its URL."""
        _data = os.urandom(1000)
        _url = "http://example.com/metafile"
        _key = url_based_hash(_url)
        (cache._base_dir_sync / _key).write_bytes(_data)
        assert cache._cache_index.commit_entry(
            CacheMeta(file_sha256=_key, url=_url, cache_size=len(_data))
        )

        body, headers = await cache.retrieve_file(
            _url, CIMultiDict({HEADER_RANGE: "bytes=400-"})
        )

        assert await _collect(body) == _data[400:]
        assert headers[HEADER_CONTENT_RANGE] == "bytes 400-999/1000"

    async def test_with_the_cache_disabled_a_range_goes_to_the_remote(
        self, tmp_path_factory: pytest.TempPathFactory
    ):
        _cache = OTACache(
            cache_enabled=False,
            init_cache=False,
            base_dir=tmp_path_factory.mktemp("ota-cache") / "local_cache",
        )
        with patch.object(
            _cache, "_retrieve_file_by_downloading", new_callable=AsyncMock
        ) as mock_remote:
            mock_remote.return_value = (AsyncMock(), CIMultiDict({"from": "remote"}))
            await _cache.start()
            try:
                _, headers = await _cache.retrieve_file(
                    "http://example.com/file", _ranged_request("a" * 64, 100)
                )
            finally:
                await _cache.close()
        assert headers["from"] == "remote"


class TestOTACacheRangeFromExternalCache:
    """The external device cache and the NFS cache hold files whole, plain or
    zstd-compressed; a range is answered from a plain copy, and a compressed copy is
    answered whole, as a whole-file request is."""

    @pytest.fixture
    async def cache(self, tmp_path_factory: pytest.TempPathFactory):
        _cache = OTACache(
            cache_enabled=True,
            init_cache=False,
            base_dir=tmp_path_factory.mktemp("ota-cache") / "local_cache",
        )
        _device = (
            tmp_path_factory.mktemp("external_cache") / cfg.EXTERNAL_CACHE_DATA_DNAME
        )
        _nfs = tmp_path_factory.mktemp("nfs_cache") / cfg.EXTERNAL_CACHE_DATA_DNAME
        _device.mkdir()
        _nfs.mkdir()
        _cache._external_cache_data_dir = anyio.Path(_device)
        _cache._external_nfs_cache_data_dir = anyio.Path(_nfs)
        _cache.device_dir, _cache.nfs_dir = _device, _nfs  # type: ignore[attr-defined]
        with patch.object(
            _cache, "_retrieve_file_by_downloading", new_callable=AsyncMock
        ) as mock_remote:
            mock_remote.return_value = (AsyncMock(), CIMultiDict({"from": "remote"}))
            _cache.mock_remote = mock_remote  # type: ignore[attr-defined]
            await _cache.start()
            try:
                yield _cache
            finally:
                await _cache.close()

    @pytest.mark.parametrize("source", ["device", "nfs"])
    async def test_the_rest_of_a_plain_copy_comes_from_the_external_cache(
        self, cache: OTACache, source: str
    ):
        _data, _sha = os.urandom(cfg.LOCAL_READ_SIZE + 123), "c" * 64
        _dir = cache.device_dir if source == "device" else cache.nfs_dir  # type: ignore[attr-defined]
        (_dir / _sha).write_bytes(_data)
        _start = len(_data) // 2

        body, headers = await cache.retrieve_file(
            "http://example.com/file", _ranged_request(_sha, _start)
        )

        assert await _collect(body) == _data[_start:]
        assert (
            headers[HEADER_CONTENT_RANGE]
            == f"bytes {_start}-{len(_data) - 1}/{len(_data)}"
        )
        assert headers[HEADER_OTA_FILE_CACHE_CONTROL] == export_kwargs_as_header_string(
            file_sha256=_sha
        )
        cache.mock_remote.assert_not_called()  # type: ignore[attr-defined]
        _hits = cache._metrics_data
        assert (_hits.cache_external_hits, _hits.cache_external_nfs_hits) == (
            (1, 0) if source == "device" else (0, 1)
        )

    async def test_a_compressed_copy_is_answered_whole_as_a_whole_file_is(
        self, cache: OTACache
    ):
        """The client's offset is into the decompressed bytes, which the compressed
        copy cannot be read from; the whole copy, compressed, is what a whole-file
        request gets, and the client reads the 200 as "start again" -- from here."""
        _data, _sha = os.urandom(5000), "d" * 64
        _zst = zstandard.ZstdCompressor().compress(_data)
        (
            cache.device_dir / f"{_sha}.{cfg.EXTERNAL_CACHE_STORAGE_COMPRESS_ALG}"
        ).write_bytes(_zst)  # type: ignore[attr-defined]

        body, headers = await cache.retrieve_file(
            "http://example.com/file", _ranged_request(_sha, 2500)
        )

        assert await _collect(body) == _zst
        assert HEADER_CONTENT_RANGE not in headers
        assert (
            f"file_compression_alg={cfg.EXTERNAL_CACHE_STORAGE_COMPRESS_ALG}"
            in headers[HEADER_OTA_FILE_CACHE_CONTROL]
        )
        cache.mock_remote.assert_not_called()  # type: ignore[attr-defined]

    async def test_the_sources_are_tried_in_the_order_a_whole_file_is(
        self, cache: OTACache
    ):
        """External device, then the local cache, then NFS."""
        _sha = "e" * 64
        _device, _local, _nfs = (os.urandom(1000) for _ in range(3))
        (cache.nfs_dir / _sha).write_bytes(_nfs)  # type: ignore[attr-defined]
        (cache._base_dir_sync / _sha).write_bytes(_local)
        assert cache._cache_index.commit_entry(
            CacheMeta(file_sha256=_sha, url="http://example.com/file", cache_size=1000)
        )

        body, _ = await cache.retrieve_file(
            "http://example.com/file", _ranged_request(_sha, 100)
        )
        assert await _collect(body) == _local[100:], "local before NFS"

        (cache.device_dir / _sha).write_bytes(_device)  # type: ignore[attr-defined]
        body, _ = await cache.retrieve_file(
            "http://example.com/file", _ranged_request(_sha, 100)
        )
        assert await _collect(body) == _device[100:], "the device cache before local"

    async def test_a_start_past_a_plain_copy_is_left_to_the_remote(
        self, cache: OTACache
    ):
        _sha = "f" * 64
        (cache.device_dir / _sha).write_bytes(os.urandom(1000))  # type: ignore[attr-defined]

        _, headers = await cache.retrieve_file(
            "http://example.com/file", _ranged_request(_sha, 1000)
        )

        assert headers["from"] == "remote"

    async def test_the_external_cache_serves_a_range_with_the_local_cache_disabled(
        self, tmp_path_factory: pytest.TempPathFactory
    ):
        """As for a whole file: an external cache is used even when local caching is
        off (NOTE(20241202) in retrieve_file)."""
        _cache = OTACache(
            cache_enabled=False,
            init_cache=False,
            base_dir=tmp_path_factory.mktemp("ota-cache") / "local_cache",
        )
        _device = (
            tmp_path_factory.mktemp("external_cache") / cfg.EXTERNAL_CACHE_DATA_DNAME
        )
        _device.mkdir()
        _cache._external_cache_data_dir = anyio.Path(_device)
        _data, _sha = os.urandom(1000), "a" * 64
        (_device / _sha).write_bytes(_data)
        with patch.object(
            _cache, "_retrieve_file_by_downloading", new_callable=AsyncMock
        ) as mock_remote:
            await _cache.start()
            try:
                body, headers = await _cache.retrieve_file(
                    "http://example.com/file", _ranged_request(_sha, 300)
                )
                assert await _collect(body) == _data[300:]
            finally:
                await _cache.close()
        assert headers[HEADER_CONTENT_RANGE] == "bytes 300-999/1000"
        mock_remote.assert_not_called()


class TestOTACacheNfsCacheLookupOrder:
    """Tests for NFS vs local cache lookup ordering in retrieve_file."""

    async def test_local_cache_checked_before_nfs(
        self, tmp_path_factory: pytest.TempPathFactory
    ):
        """Local cache lookup must be attempted before NFS cache."""
        nfs_mount = tmp_path_factory.mktemp("nfs_cache")

        with patch(
            "ota_proxy.ota_cache.cmdhelper.is_target_mounted", return_value=True
        ):
            cache = OTACache(
                cache_enabled=True,
                init_cache=False,
                base_dir=tmp_path_factory.mktemp("ota-cache") / "local_cache",
                external_nfs_cache_mnt_point=str(nfs_mount),
            )

            nfs_result = (AsyncMock(), CIMultiDict({"source": "nfs"}))

            with (
                patch.object(
                    cache, "_retrieve_file_by_cache_lookup", new_callable=AsyncMock
                ) as mock_local,
                patch.object(
                    cache, "_retrieve_file_by_external_cache", new_callable=AsyncMock
                ) as mock_nfs,
                patch.object(
                    cache, "_retrieve_file_by_downloading", new_callable=AsyncMock
                ),
            ):
                mock_local.return_value = None
                mock_nfs.return_value = nfs_result

                await cache.start()
                try:
                    result = await cache.retrieve_file(
                        "http://example.com/file", CIMultiDict()
                    )

                    mock_local.assert_called_once()
                    mock_nfs.assert_called_once()
                    assert result == nfs_result
                finally:
                    await cache.close()

    async def test_local_cache_preferred_over_nfs(
        self, tmp_path_factory: pytest.TempPathFactory
    ):
        """Local cache hit must short-circuit NFS cache lookup."""
        cache = OTACache(
            cache_enabled=True,
            init_cache=False,
            base_dir=tmp_path_factory.mktemp("ota-cache") / "local_cache",
            external_nfs_cache_mnt_point="/mnt/nfs_cache",
        )

        local_result = (AsyncMock(), CIMultiDict({"source": "local"}))
        nfs_result = (AsyncMock(), CIMultiDict({"source": "nfs"}))

        with (
            patch.object(
                cache, "_retrieve_file_by_cache_lookup", new_callable=AsyncMock
            ) as mock_local,
            patch.object(
                cache, "_retrieve_file_by_external_cache", new_callable=AsyncMock
            ) as mock_nfs,
            patch.object(
                cache, "_retrieve_file_by_downloading", new_callable=AsyncMock
            ),
        ):
            mock_local.return_value = local_result
            mock_nfs.return_value = nfs_result

            await cache.start()
            try:
                result = await cache.retrieve_file(
                    "http://example.com/file", CIMultiDict()
                )

                mock_local.assert_called_once()
                mock_nfs.assert_not_called()
                assert result == local_result
            finally:
                await cache.close()

    async def test_local_cache_hit_skips_nfs(
        self, tmp_path_factory: pytest.TempPathFactory
    ):
        """When only local cache has the file, NFS should not be consulted."""
        cache = OTACache(
            cache_enabled=True,
            init_cache=False,
            base_dir=tmp_path_factory.mktemp("ota-cache") / "local_cache",
            external_nfs_cache_mnt_point="/mnt/nfs_cache",
        )

        local_result = (AsyncMock(), CIMultiDict({"source": "local"}))

        with (
            patch.object(
                cache, "_retrieve_file_by_cache_lookup", new_callable=AsyncMock
            ) as mock_local,
            patch.object(
                cache, "_retrieve_file_by_external_cache", new_callable=AsyncMock
            ) as mock_nfs,
            patch.object(
                cache, "_retrieve_file_by_downloading", new_callable=AsyncMock
            ),
        ):
            mock_local.return_value = local_result
            mock_nfs.return_value = None

            await cache.start()
            try:
                result = await cache.retrieve_file(
                    "http://example.com/file", CIMultiDict()
                )

                mock_local.assert_called_once()
                mock_nfs.assert_not_called()
                assert result == local_result
            finally:
                await cache.close()
