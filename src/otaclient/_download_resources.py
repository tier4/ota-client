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
from itertools import chain
from pathlib import Path
from typing import Generator, Iterable
from urllib.parse import urljoin

from ota_image_libs.v1.image_manifest.schema import ImageIdentifier
from ota_image_libs.v1.resource_table.db import (
    PrepareResourceHelper,
    ResourceTableDBHelper,
)

from ota_metadata.legacy2.metadata import (
    LegacyOTAImageResourceMeta as _legacy_ResourceMeta,
)
from ota_metadata.legacy2.rs_table import ResourceTable as _legacy_ResourceTable
from ota_metadata.legacy2.rs_table import (
    ResourceTableORMPool as _legacy_ResourceTableORMPool,
)
from ota_metadata.utils import DownloadInfo
from ota_metadata.v1 import OTAImageHelper
from otaclient_common import EMPTY_FILE_SHA256
from otaclient_common.downloader import (
    Downloader,
    DownloaderPool,
    DownloadPoolWatchdogFuncContext,
    DownloadResult,
)
from otaclient_common.retry_task_map import ThreadPoolExecutorWithRetry

DEFAULT_SHUFFLE_BATCH_SIZE = 256
RST_DB_CONNS_NUM = 3


class _BaseDownloader:
    _downloader_pool: DownloaderPool
    _downloader_mapper: dict[int, Downloader]
    _resources_to_download: Iterable[bytes]
    _shuffle_batch_size: int

    def _downloader_worker_initializer(self) -> None:
        self._downloader_mapper[threading.get_native_id()] = (
            self._downloader_pool.get_instance()
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


class DownloadResourcesFromLegacyOTAImage(_BaseDownloader):
    def __init__(
        self,
        *,
        resource_meta: _legacy_ResourceMeta,
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

        self._downloader_pool = downloader_pool
        self._resources_to_download = resources_to_download
        self._resource_meta = resource_meta
        self._ota_metadata = resource_meta._ota_metadata

        self._downloader_threads = downloader_pool.instance_num
        # for worker thread local downloader
        self._downloader_mapper: dict[int, Downloader] = {}

    def _download_single_resource_at_thread(
        self, _digest: bytes, _rst_orm_pool: _legacy_ResourceTableORMPool
    ) -> DownloadResult:
        _entry: _legacy_ResourceTable = _rst_orm_pool.orm_select_entry(digest=_digest)
        _download_info = self._resource_meta.get_download_info(_entry)
        if (_digest := _entry.digest) == EMPTY_FILE_SHA256:
            return DownloadResult(0, 0, 0)

        downloader = self._downloader_mapper[threading.get_native_id()]
        # NOTE: currently download only use sha256
        return downloader.download(
            url=_download_info.url,
            dst=_download_info.dst,
            digest=_digest.hex(),
            size=_download_info.original_size,
            compression_alg=_download_info.compression_alg,
        )

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
            ) as _mapper, _legacy_ResourceTableORMPool(
                con_factory=self._ota_metadata.connect_rstable,
                number_of_cons=self._db_conns_num,
            ) as _rst_orm_pool:
                _bound_download_func = partial(
                    self._download_single_resource_at_thread,
                    _rst_orm_pool=_rst_orm_pool,
                )
                for _fut in _mapper.ensure_tasks(
                    _bound_download_func, self._iter_digests_with_shuffle()
                ):
                    yield _fut
        finally:
            self._downloader_pool.release_all_instances()


class DownloadResources(_BaseDownloader):
    def __init__(
        self,
        resource_db_helper: ResourceTableDBHelper,
        *,
        resources_to_download: Iterable[bytes],
        blob_storage_base_url: str,
        resource_dir: Path,
        downloader_pool: DownloaderPool,
        db_conns_num: int = RST_DB_CONNS_NUM,
        max_concurrent: int,
        download_inactive_timeout: int,
        shuffle_batch_size: int = DEFAULT_SHUFFLE_BATCH_SIZE,
    ) -> None:
        self._rst_db_helper = resource_db_helper

        self._db_conns_num = db_conns_num
        self._download_inactive_timeout = download_inactive_timeout
        self._shuffle_batch_size = shuffle_batch_size
        self._max_concurrent = max_concurrent

        self._downloader_pool = downloader_pool
        self._resource_dir = resource_dir
        self._base_url = f"{blob_storage_base_url.rstrip('/')}/"
        self._resources_to_download = resources_to_download

        self._downloader_threads = downloader_pool.instance_num
        # for worker thread local downloader
        self._downloader_mapper = {}

    def _download_single_resource_at_thread(
        self, _digest: bytes, _rst_helper: PrepareResourceHelper
    ) -> DownloadResult:
        downloader = self._downloader_mapper[threading.get_native_id()]
        save_dst = self._resource_dir / _digest.hex()

        _entry, _gen = _rst_helper.prepare_resource_at_thread(_digest, save_dst)
        _res = DownloadResult(download_size=_entry.size)

        for _requested_blob, _tmp_save_dst in _gen:
            _this_res = downloader.download(
                url=urljoin(self._base_url, _requested_blob),
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

    def download_resources(self) -> Generator[Future[DownloadResult]]:
        """
        NOTE: this method will open up a threadpool for downloading, each thread worker
              will get a downloader instance from the downloader pool, and release all
              downloader instances after finish.
        """
        try:
            _rst_orm_pool = self._rst_db_helper.get_orm_pool(self._db_conns_num)
            _rst_helper = PrepareResourceHelper(_rst_orm_pool, self._resource_dir)
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
            ) as _mapper, _rst_orm_pool:
                _bound_download_func = partial(
                    self._download_single_resource_at_thread,
                    _rst_helper=_rst_helper,
                )
                for _fut in _mapper.ensure_tasks(
                    _bound_download_func, self._iter_digests_with_shuffle()
                ):
                    yield _fut
        finally:
            self._downloader_pool.release_all_instances()


class DownloadOTAImageMeta(_BaseDownloader):
    def __init__(
        self,
        ota_image_helper: OTAImageHelper,
        *,
        downloader_pool: DownloaderPool,
        max_concurrent: int,
        download_inactive_timeout: int,
    ) -> None:
        self._ota_image_helper = ota_image_helper
        self._download_inactive_timeout = download_inactive_timeout
        self._max_concurrent = max_concurrent

        self._downloader_pool = downloader_pool
        self._downloader_threads = downloader_pool.instance_num
        # for worker thread local downloader
        self._downloader_mapper = {}

    def _downloader_worker_initializer(self) -> None:
        self._downloader_mapper[threading.get_native_id()] = (
            self._downloader_pool.get_instance()
        )

    def _download_with_condition_at_thread(
        self,
        entries: DownloadInfo | list[DownloadInfo],
        *,
        condition: threading.Condition,
    ) -> DownloadResult:
        """Download a single OTA image metadata file.

        Just a wrapper around _download_file method.

        Returns:
            Retry counts, downloaded files size and traffic on wire.
        """
        downloader = self._downloader_mapper[threading.get_native_id()]

        _retry_count, _download_size, _traffic_on_wire = 0, 0, 0
        if not isinstance(entries, list):
            entries = [entries]

        with condition:
            for entry in entries:
                _res = downloader.download(
                    url=entry.url,
                    dst=entry.dst,
                    digest=entry.digest,
                    size=entry.original_size,
                    # NOTE: the OTAImageHelper will do the decompression for us,
                    #       do not enable auto decompression for the downloader.
                )
                _retry_count += _res.retry_count
                _download_size += _res.download_size
                _traffic_on_wire += _res.traffic_on_wire

            condition.notify()  # notify the metadata generator that this batch of download is finished
        return DownloadResult(_retry_count, _download_size, _traffic_on_wire)

    def download_ota_image_meta(
        self, _image_payload_to_select: ImageIdentifier
    ) -> Generator[Future[DownloadResult]]:
        """
        NOTE: this method will open up a threadpool for downloading, each thread worker
              will get a downloader instance from the downloader pool, and release all
              downloader instances after finish.
        """
        try:
            _condition = threading.Condition()
            _to_download = chain(
                self._ota_image_helper.download_and_verify_image_index(_condition),
                self._ota_image_helper.select_image_payload(
                    _image_payload_to_select, _condition
                ),
            )

            with ThreadPoolExecutorWithRetry(
                max_concurrent=self._max_concurrent,
                max_workers=self._downloader_threads,
                thread_name_prefix="download_ota_image_meta",
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
                _bound_download_func = partial(
                    self._download_with_condition_at_thread,
                    condition=_condition,
                )
                for _fut in _mapper.ensure_tasks(_bound_download_func, _to_download):
                    yield _fut
        finally:
            self._downloader_pool.release_all_instances()
