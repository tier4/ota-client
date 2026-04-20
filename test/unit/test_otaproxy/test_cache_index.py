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

import bisect
import time

import pytest
from multidict import CIMultiDictProxy

from ota_proxy._consts import HEADER_CONTENT_ENCODING, HEADER_OTA_FILE_CACHE_CONTROL
from ota_proxy.cache_index import CacheDBWriter, _build_index_entry_headers
from ota_proxy.config import config as cfg
from ota_proxy.db import CacheMeta, init_db
from ota_proxy.utils import url_based_hash

_REAL_SHA256 = "a" * 64
_URL_HASH = url_based_hash("http://example.com/f")

BUCKET_SIZE_LIST = list(cfg.BUCKET_FILE_SIZE_DICT)


def _expected_bucket_idx(cache_size: int) -> int:
    """Reference implementation matching old LRUCacheHelper logic."""
    return bisect.bisect_right(BUCKET_SIZE_LIST, cache_size) - 1


#
# ------------ Tests: bucket_idx backward compat ------------ #
#


# fmt: off
BUCKET_IDX_CASES = [
    # (cache_size, expected_bucket_idx, description)
    (0,                 0, "zero-size file -> first bucket"),
    (512,               0, "under 1KiB -> first bucket"),
    (1024,              1, "exactly 1KiB boundary"),
    (1025,              1, "just above 1KiB"),
    (2048,              2, "exactly 2KiB boundary"),
    (3000,              2, "between 2KiB and 4KiB"),
    (4096,              3, "exactly 4KiB boundary"),
    (8192,              4, "exactly 8KiB boundary"),
    (16384,             5, "exactly 16KiB boundary"),
    (32768,             6, "exactly 32KiB boundary"),
    (100_000,           6, "between 32KiB and 256KiB"),
    (256 * 1024,        7, "exactly 256KiB boundary"),
    (1024**2,           8, "exactly 1MiB boundary"),
    (8 * 1024**2,       9, "exactly 8MiB boundary"),
    (16 * 1024**2,     10, "exactly 16MiB boundary"),
    (32 * 1024**2,     11, "exactly 32MiB -> last bucket"),
    (100 * 1024**2,    11, "above 32MiB -> last bucket"),
]
# fmt: on


class TestBucketIdxCalculation:
    """Verify bucket_idx matches old LRUCacheHelper.commit_entry logic."""

    @pytest.mark.parametrize(
        "cache_size, expected_idx, desc",
        BUCKET_IDX_CASES,
        ids=[c[2] for c in BUCKET_IDX_CASES],
    )
    def test_bucket_idx_matches_old_lru(
        self,
        tmp_path,
        cache_size: int,
        expected_idx: int,
        desc: str,
    ):
        """CacheDBWriter._flush_to_db must assign the same bucket_idx as old LRU."""
        db_f = tmp_path / "cache.db"
        init_db(db_f, cfg.TABLE_NAME)

        writer = CacheDBWriter(db_f)
        entry = CacheMeta(
            file_sha256=url_based_hash(f"http://example.com/{cache_size}"),
            url=f"http://example.com/{cache_size}",
            cache_size=cache_size,
            last_access=int(time.time()),
        )

        writer._flush_to_db([entry], [])
        writer._orm.orm_con.close()

        assert entry.bucket_idx == expected_idx
        assert entry.bucket_idx == _expected_bucket_idx(cache_size)


#
# ------------ Tests: _build_index_entry_headers ------------ #
#


class TestBuildIndexEntryHeaders:
    """Pre-computed CacheIndexEntry.headers must match the legacy export_headers
    output: content-encoding only when set, ota-file-cache-control only for
    real sha256 hashes (not URL-based), and the result must be a read-only
    CIMultiDictProxy so it can be safely shared across requests."""

    def test_returns_cimultidictproxy(self):
        res = _build_index_entry_headers(
            _REAL_SHA256, content_encoding=None, file_compression_alg=None
        )
        assert isinstance(res, CIMultiDictProxy)

    def test_real_sha256_without_compression_sets_cache_control_only(self):
        res = _build_index_entry_headers(
            _REAL_SHA256, content_encoding=None, file_compression_alg=None
        )
        assert HEADER_CONTENT_ENCODING not in res
        cache_ctrl = res[HEADER_OTA_FILE_CACHE_CONTROL]
        assert f"file_sha256={_REAL_SHA256}" in cache_ctrl

    def test_real_sha256_with_compression_embeds_alg_in_cache_control(self):
        res = _build_index_entry_headers(
            _REAL_SHA256, content_encoding="zstd", file_compression_alg="zst"
        )
        assert res[HEADER_CONTENT_ENCODING] == "zstd"
        cache_ctrl = res[HEADER_OTA_FILE_CACHE_CONTROL]
        assert f"file_sha256={_REAL_SHA256}" in cache_ctrl
        assert "file_compression_alg=zst" in cache_ctrl

    def test_url_based_hash_omits_cache_control(self):
        """URL-based hash entries must not expose ota-file-cache-control."""
        res = _build_index_entry_headers(
            _URL_HASH, content_encoding="gzip", file_compression_alg=None
        )
        assert HEADER_OTA_FILE_CACHE_CONTROL not in res
        assert res[HEADER_CONTENT_ENCODING] == "gzip"

    def test_empty_file_sha256_omits_cache_control(self):
        res = _build_index_entry_headers(
            "", content_encoding=None, file_compression_alg=None
        )
        assert HEADER_OTA_FILE_CACHE_CONTROL not in res
        assert HEADER_CONTENT_ENCODING not in res


#
# ------------ Tests: CacheMeta.export_headers_to_client ------------ #
#


class TestCacheMetaExportHeaders:
    """CacheMeta.export_headers_to_client must mirror _build_index_entry_headers
    and must return a read-only CIMultiDictProxy to match the ota_cache.py
    return-type contract."""

    def test_returns_cimultidictproxy(self):
        meta = CacheMeta(file_sha256=_REAL_SHA256, url="http://example.com/f")
        assert isinstance(meta.export_headers_to_client(), CIMultiDictProxy)

    def test_url_based_hash_has_no_cache_control(self):
        meta = CacheMeta(
            file_sha256=_URL_HASH, url="http://example.com/f", content_encoding="gzip"
        )
        res = meta.export_headers_to_client()
        assert HEADER_OTA_FILE_CACHE_CONTROL not in res
        assert res[HEADER_CONTENT_ENCODING] == "gzip"

    def test_real_sha256_emits_cache_control_with_compression(self):
        meta = CacheMeta(
            file_sha256=_REAL_SHA256,
            url="http://example.com/f",
            file_compression_alg="zst",
        )
        cache_ctrl = meta.export_headers_to_client()[HEADER_OTA_FILE_CACHE_CONTROL]
        assert f"file_sha256={_REAL_SHA256}" in cache_ctrl
        assert "file_compression_alg=zst" in cache_ctrl
