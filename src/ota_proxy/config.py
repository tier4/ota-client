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


import contextlib
import os
import sqlite3
from functools import cached_property

from simple_sqlite3_orm import utils


class Config:
    BASE_DIR = "/ota-cache"

    # ------ io config ------ #
    REMOTE_READ_CHUNK_SIZE = 512 * 1024  # 512KiB
    WRITE_CHUNK_SIZE = 2 * 1024 * 1024  # 2MiB
    LOCAL_READ_SIZE = 2 * 1024 * 1024  # 2MiB

    # 8~10 write threads
    CACHE_WRITE_WORKERS_NUM = max(8, min(10, (os.cpu_count() or 1) + 4))
    # 10~12 read threads
    CACHE_READ_WORKERS_NUM = max(10, min(12, (os.cpu_count() or 1) + 4))

    # NOTE(20251015): enlarge the rw backlog's length as previous settings
    #                 are too conservative and unmatching the r/w worker threads
    #                 numbers. With testing, confirm the following settings
    #                 will not cause write backlog overflow while otaproxy's
    #                 memory usage is still normal and constrained.
    MAX_PENDING_WRITE = 384
    MAX_PENDING_READ = 512

    # ------ storage quota ------ #
    DISK_USE_LIMIT_SOFT_P = 70  # in p%
    DISK_USE_LIMIT_HARD_P = 80  # in p%
    DISK_USE_PULL_INTERVAL = 2  # in seconds

    # ------ metrics config ------ #
    METRICS_UPDATE_INTERVAL = 5  # in seconds

    # ------ LRU cache config ------ #
    # value is the largest numbers of files that
    # might need to be deleted for the bucket to hold a new entry
    # if we have to reserve space for this file.
    # bucket definition: revision 2
    BUCKET_FILE_SIZE_DICT = {
        0: 0,  # [0, 1KiB), will not be rotated
        1 * 1024: 1,  # [1KiB, 2KiB)
        2 * 1024: 1,
        4 * 1024: 2,
        8 * 1024: 2,
        16 * 1024: 2,  # 16KiB
        32 * 1024: 2,
        256 * 1024: 8,  # 256KiB
        1 * (1024**2): 4,  # 1MiB
        8 * (1024**2): 8,
        16 * (1024**2): 2,
        32 * (1024**2): 2,  # [32MiB, ~), will not be rotated
    }

    # ------ db config ------ #
    DB_FILE = f"{BASE_DIR}/cache_db"
    # serializing the access
    READ_DB_THREADS = WRITE_DB_THREAD = 1
    DB_THREAD_WAIT_TIMEOUT = 30  # seconds

    # ota-cache table
    # NOTE: use table name to keep track of table scheme version
    TABLE_DEFINITION_VERSION = "v4"
    TABLE_NAME = f"ota_cache_{TABLE_DEFINITION_VERSION}"

    # cache streaming behavior
    AIOHTTP_SOCKET_READ_TIMEOUT = 16  # second

    TMP_FILE_PREFIX = "tmp"
    URL_BASED_HASH_PREFIX = "URL_"

    # ------ external cache ------ #
    # the file extension for compressed files in external cache storage
    EXTERNAL_CACHE_STORAGE_COMPRESS_ALG = "zst"

    EXTERNAL_CACHE_DEV_FSLABEL = "ota_cache_src"
    EXTERNAL_CACHE_DATA_DNAME = "data"
    """The cache blob storage is located at <cache_mnt_point>/data."""

    # ------ task management ------ #
    MAX_CONCURRENT_REQUESTS = 1024


class _Sqlite3FeatureFlags:
    # RETURNING statement is available only after sqlite3 v3.35.0
    RETURNING_AVAILABLE = sqlite3.sqlite_version_info >= (3, 35, 0)
    STRICT_AVAILABLE = sqlite3.sqlite_version_info >= (3, 37, 0)

    @cached_property
    def SQLITE_ENABLE_UPDATE_DELETE_LIMIT(self) -> bool:
        try:
            with contextlib.closing(sqlite3.connect(":memory:")) as conn:
                return bool(
                    utils.check_pragma_compile_time_options(
                        conn, "SQLITE_ENABLE_UPDATE_DELETE_LIMIT"
                    )
                )
        except Exception:
            return False


sqlite3_feature_flags = _Sqlite3FeatureFlags()
config = Config()
