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
import sqlite3
import time

import pytest

from ota_proxy.cache_index import CacheDBWriter, CacheIndex
from ota_proxy.config import config as cfg
from ota_proxy.db import CacheMeta, CacheMetaORM, init_db
from ota_proxy.utils import url_based_hash

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
        """CacheDBWriter._flush must assign the same bucket_idx as old LRU."""
        db_f = tmp_path / "cache.db"
        init_db(db_f, cfg.TABLE_NAME)

        writer = CacheDBWriter(db_f)
        entry = CacheMeta(
            file_sha256=url_based_hash(f"http://example.com/{cache_size}"),
            url=f"http://example.com/{cache_size}",
            cache_size=cache_size,
            last_access=int(time.time()),
        )

        writer._flush([entry])
        writer._orm.orm_con.close()

        assert entry.bucket_idx == expected_idx
        assert entry.bucket_idx == _expected_bucket_idx(cache_size)


class TestBucketIdxPersistedToDB:
    """Verify bucket_idx is persisted correctly through the full pipeline."""

    @pytest.fixture()
    def cache_index(self, tmp_path):
        base_dir = tmp_path / "ota-cache"
        base_dir.mkdir()
        db_f = tmp_path / "cache.db"
        idx = CacheIndex(db_f, base_dir, init_db=True)
        try:
            yield idx, db_f, base_dir
        finally:
            idx.close()

    def test_committed_entries_have_bucket_idx_in_db(self, cache_index):
        """Entries committed via CacheIndex must have correct bucket_idx in DB."""
        idx, db_f, base_dir = cache_index

        test_sizes = [0, 1024, 4096, 256 * 1024, 1024**2, 32 * 1024**2]
        entries = []
        for size in test_sizes:
            url = f"http://example.com/file_{size}"
            sha = url_based_hash(url)
            entry = CacheMeta(
                file_sha256=sha, url=url, cache_size=size, last_access=int(time.time())
            )
            (base_dir / sha).touch()
            idx.commit_entry(entry)
            entries.append(entry)

        # Wait for the DB writer to flush
        idx.close()

        # Read back from DB and verify bucket_idx
        with sqlite3.connect(db_f) as con:
            orm = CacheMetaORM(con, cfg.TABLE_NAME)
            for entry in entries:
                rows = list(
                    orm.orm_select_entries(
                        _stmt=(
                            f"SELECT * FROM {cfg.TABLE_NAME} "
                            f"WHERE file_sha256 = '{entry.file_sha256}'"
                        ),
                    )
                )
                assert len(rows) == 1, f"expected 1 row for {entry.file_sha256}"

                expected = _expected_bucket_idx(entry.cache_size)
                assert rows[0].bucket_idx == expected, (
                    f"cache_size={entry.cache_size}: "
                    f"got bucket_idx={rows[0].bucket_idx}, expected={expected}"
                )
