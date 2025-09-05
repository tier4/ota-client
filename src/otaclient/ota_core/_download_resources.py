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

import contextlib
import random
import threading
import time
from concurrent.futures import Future
from functools import partial
from pathlib import Path
from typing import Generator, Iterable
from urllib.parse import urljoin

from ota_image_libs.v1.resource_table.db import (
    PrepareResourceHelper,
    ResourceTableDBHelper,
)

from ota_metadata.legacy2.metadata import ResourceMeta
from otaclient_common import EMPTY_FILE_SHA256_BYTE
from otaclient_common.download_info import DownloadInfo
from otaclient_common.downloader import (
    Downloader,
    DownloaderPool,
    DownloadPoolWatchdogFuncContext,
    DownloadResult,
)
from otaclient_common.retry_task_map import ThreadPoolExecutorWithRetry

DEFAULT_SHUFFLE_BATCH_SIZE = 256


class _BaseDownloadHelper:
    def __init__(
        self,
        *,
        downloader_pool: DownloaderPool,
        max_concurrent: int,
        download_inactive_timeout: int,
        shuffle_batch_size: int = DEFAULT_SHUFFLE_BATCH_SIZE,
    ) -> None:
        self._download_inactive_timeout = download_inactive_timeout
        self._shuffle_batch_size = shuffle_batch_size
        self._max_concurrent = max_concurrent

        # NOTE: caller takes the responsibility to close the downloader_pool
        self._downloader_pool = downloader_pool

        self._downloader_threads = downloader_pool.instance_num
        # for worker thread local downloader
        self._downloader_mapper: dict[int, Downloader] = {}

    def _downloader_worker_initializer(self) -> None:
        self._downloader_mapper[threading.get_native_id()] = (
            self._downloader_pool.get_instance()
        )

    @contextlib.contextmanager
    def _downloader_pool_with_retry(self, thread_name_prefix: str):
        try:
            with ThreadPoolExecutorWithRetry(
                max_concurrent=self._max_concurrent,
                max_workers=self._downloader_threads,
                thread_name_prefix=thread_name_prefix,
                initializer=self._downloader_worker_initializer,
                watchdog_func=partial(
                    self._downloader_pool.downloading_watchdog,
                    ctx=DownloadPoolWatchdogFuncContext(
                        downloaded_bytes=0,
                        previous_active_timestamp=int(time.time()),
                    ),
                    max_idle_timeout=self._download_inactive_timeout,
                ),
            ) as _pool:
                yield _pool
        finally:
            self._downloader_pool.release_all_instances()

    def _download_with_condition_at_thread(
        self, _to_download: list[DownloadInfo], condition: threading.Condition
    ) -> list[DownloadResult]:
        downloader = self._downloader_mapper[threading.get_native_id()]
        _res = []
        with condition:
            for _download_info in _to_download:
                _res.append(
                    downloader.download(
                        url=_download_info.url,
                        dst=_download_info.dst,
                        digest=_download_info.digest,
                        size=_download_info.original_size,
                        compression_alg=_download_info.compression_alg,
                    )
                )
            condition.notify()
        return _res

    def download_meta_files(
        self,
        _download_info: Generator[list[DownloadInfo]],
        condition: threading.Condition,
    ) -> Generator[Future[list[DownloadResult]]]:
        with self._downloader_pool_with_retry(
            "download_ota_image_metafiles"
        ) as _mapper:
            for _fut in _mapper.ensure_tasks(
                partial(
                    self._download_with_condition_at_thread,
                    condition=condition,
                ),
                _download_info,
            ):
                yield _fut

    def _iter_digests_with_shuffle(self, _digests: Iterable[bytes]) -> Generator[bytes]:
        """Shuffle the input digests to avoid multiple ECUs downloading
        the same resources at the same time."""
        _cur_batch = []
        for _digest in _digests:
            _cur_batch.append(_digest)
            if len(_cur_batch) >= self._shuffle_batch_size:
                random.shuffle(_cur_batch)
                yield from _cur_batch
                _cur_batch.clear()
        if _cur_batch:
            random.shuffle(_cur_batch)
            yield from _cur_batch


class DownloadHelperForLegacyOTAImage(_BaseDownloadHelper):
    def _download_single_resource_at_thread(
        self, _digest: bytes, resource_meta: ResourceMeta
    ) -> DownloadResult:
        if _digest == EMPTY_FILE_SHA256_BYTE:
            return DownloadResult(0, 0, 0)

        _download_info = resource_meta.get_download_info(_digest)
        downloader = self._downloader_mapper[threading.get_native_id()]
        # NOTE: currently download only use sha256
        return downloader.download(
            url=_download_info.url,
            dst=_download_info.dst,
            digest=_digest.hex(),
            size=_download_info.original_size,
            compression_alg=_download_info.compression_alg,
        )

    def download_resources(
        self, resources_to_download: Iterable[bytes], resource_meta: ResourceMeta
    ) -> Generator[Future[DownloadResult]]:
        with self._downloader_pool_with_retry("download_ota_resources") as _mapper:
            for _fut in _mapper.ensure_tasks(
                partial(
                    self._download_single_resource_at_thread,
                    resource_meta=resource_meta,
                ),
                self._iter_digests_with_shuffle(resources_to_download),
            ):
                yield _fut


RESOURCE_TABLE_DB_CONN = 3


class DownloadHelperForOTAImageV1(_BaseDownloadHelper):
    def _download_single_resource_at_thread(
        self, _digest: bytes, *, _rst_helper: PrepareResourceHelper, _base_url: str
    ) -> DownloadResult:
        downloader = self._downloader_mapper[threading.get_native_id()]

        _entry, _gen = _rst_helper.prepare_resource_at_thread(_digest)
        _res = DownloadResult(download_size=_entry.size)
        for _requested_blob, _tmp_save_dst in _gen:
            _this_res = downloader.download(
                url=urljoin(_base_url, _requested_blob),
                dst=_tmp_save_dst,
                digest=_requested_blob,
                # NOTE: do not do auto decompression, the PrepareResourceHelper
                #       will do the decompression.
            )
            _res.retry_count += _this_res.retry_count
            _res.traffic_on_wire += _this_res.traffic_on_wire
        # after all the blobs are prepare, the `prepare_resource_at_thread` method will
        #   rebuild the requested origin blob.
        return _res

    def download_resources2(
        self,
        resources_to_download: Iterable[bytes],
        resource_db_helper: ResourceTableDBHelper,
        *,
        blob_storage_base_url: str,
        resource_dir: Path,
        download_tmp_dir: Path,
    ) -> Generator[Future[DownloadResult]]:
        _base_url = f"{blob_storage_base_url.rstrip('/')}/"
        _rst_orm_pool = resource_db_helper.get_orm_pool(RESOURCE_TABLE_DB_CONN)
        _rst_helper = PrepareResourceHelper(
            _rst_orm_pool,
            resource_dir=resource_dir,
            download_tmp_dir=download_tmp_dir,
        )

        with self._downloader_pool_with_retry("download_ota_resources") as _mapper:
            for _fut in _mapper.ensure_tasks(
                partial(
                    self._download_single_resource_at_thread,
                    _rst_helper=_rst_helper,
                    _base_url=_base_url,
                ),
                self._iter_digests_with_shuffle(resources_to_download),
            ):
                yield _fut
