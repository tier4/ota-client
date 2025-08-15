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

import random
import threading
import time
from concurrent.futures import Future
from functools import partial
from typing import Generator, Iterable

from ota_metadata.legacy2.metadata import ResourceMeta
from otaclient_common import EMPTY_FILE_SHA256_BYTE
from otaclient_common.downloader import (
    Downloader,
    DownloaderPool,
    DownloadPoolWatchdogFuncContext,
    DownloadResult,
)
from otaclient_common.retry_task_map import ThreadPoolExecutorWithRetry

DEFAULT_SHUFFLE_BATCH_SIZE = 256
RST_DB_CONNS_NUM = 3


class DownloadResources:
    def __init__(
        self,
        *,
        resource_meta: ResourceMeta,
        resources_to_download: Iterable[bytes],
        downloader_pool: DownloaderPool,
        db_conns_num: int = RST_DB_CONNS_NUM,
        max_concurrent: int,
        download_inactive_timeout: int,
        shuffle_batch_size: int = DEFAULT_SHUFFLE_BATCH_SIZE,
    ) -> None:
        self._db_conns_num = db_conns_num
        self._download_inactive_timeout = download_inactive_timeout
        self._shuffle_batch_size = shuffle_batch_size
        self._max_concurrent = max_concurrent

        self._resources_to_download = resources_to_download
        # NOTE: caller takes the responsibility to close the downloader
        #       and the resource_meta.
        self._downloader_pool = downloader_pool
        self._resource_meta = resource_meta

        self._downloader_threads = downloader_pool.instance_num
        # for worker thread local downloader
        self._downloader_mapper: dict[int, Downloader] = {}

    def _downloader_worker_initializer(self) -> None:
        self._downloader_mapper[threading.get_native_id()] = (
            self._downloader_pool.get_instance()
        )

    def _download_single_resource_at_thread(self, _digest: bytes) -> DownloadResult:
        if _digest == EMPTY_FILE_SHA256_BYTE:
            return DownloadResult(0, 0, 0)
        _download_info = self._resource_meta.get_download_info(_digest)
        downloader = self._downloader_mapper[threading.get_native_id()]
        # NOTE: currently download only use sha256
        return downloader.download(
            url=_download_info.url,
            dst=_download_info.dst,
            digest=_digest.hex(),
            size=_download_info.original_size,
            compression_alg=_download_info.compression_alg,
        )

    def _iter_digests_with_shuffle(self) -> Generator[bytes]:
        _cur_batch = []
        for _digest in self._resources_to_download:
            _cur_batch.append(_digest)
            if len(_cur_batch) >= self._shuffle_batch_size:
                random.shuffle(_cur_batch)
                yield from _cur_batch
                _cur_batch.clear()
        if _cur_batch:
            random.shuffle(_cur_batch)
            yield from _cur_batch

    def download_resources(self) -> Generator[Future[DownloadResult]]:
        try:
            with ThreadPoolExecutorWithRetry(
                max_concurrent=self._max_concurrent,
                max_workers=self._downloader_threads,
                thread_name_prefix="download_ota_files",
                initializer=self._downloader_worker_initializer,
                watchdog_func=partial(
                    self._downloader_pool.downloading_watchdog,
                    ctx=DownloadPoolWatchdogFuncContext(
                        downloaded_bytes=0,
                        previous_active_timestamp=int(time.time()),
                    ),
                    max_idle_timeout=self._download_inactive_timeout,
                ),
            ) as _mapper:
                for _fut in _mapper.ensure_tasks(
                    self._download_single_resource_at_thread,
                    self._iter_digests_with_shuffle(),
                ):
                    yield _fut
        finally:
            self._downloader_pool.release_all_instances()
