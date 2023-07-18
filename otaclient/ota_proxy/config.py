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


class Config:
    BASE_DIR = "/ota-cache"
    CHUNK_SIZE = 1 * 1024 * 1024  # 4MB
    DISK_USE_LIMIT_SOFT_P = 70  # in p%
    DISK_USE_LIMIT_HARD_P = 80  # in p%
    DISK_USE_PULL_INTERVAL = 2  # in seconds
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
    DB_FILE = f"{BASE_DIR}/cache_db"

    # DB configuration/setup
    # ota-cache table
    # NOTE: use table name to keep track of table scheme version
    TABLE_DEFINITION_VERSION = "v3"
    TABLE_NAME = f"ota_cache_{TABLE_DEFINITION_VERSION}"

    # cache streaming behavior
    AIOHTTP_SOCKET_READ_TIMEOUT = 60  # second
    STREAMING_BACKOFF_MAX = 30  # seconds
    STREAMING_BACKOFF_FACTOR = 0.01  # second
    STREAMING_CACHED_TMP_TIMEOUT = 10  # second

    TMP_FILE_PREFIX = "tmp"
    URL_BASED_HASH_PREFIX = "URL_"

    # the file extension for compressed files in external cache storage
    EXTERNAL_CACHE_STORAGE_COMPRESS_ALG = "zst"


config = Config()
