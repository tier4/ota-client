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
import os
import random
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Generator, Iterable
from urllib.parse import urljoin

from ota_image_libs.v1.resource_table.db import ResourceTableDBHelper
from ota_image_libs.v1.resource_table.utils import PrepareResourceHelper

from ota_metadata.legacy2.metadata import ResourceMeta
from otaclient_common import EMPTY_FILE_SHA256_BYTE, SHA256DIGEST_HEX_LEN
from otaclient_common._io import file_sha256_2, remove_file
from otaclient_common.download_info import DownloadInfo
from otaclient_common.downloader import (
    Downloader,
    DownloaderPool,
    DownloadPoolWatchdogFuncContext,
    DownloadResult,
)
from otaclient_common.retry_task_map import (
    TasksEnsureFailed,
    ThreadPoolExecutorWithRetry,
)

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
        """
        Raises:
            Last exception that interrupts the work pool, or TaskEnsureFailed if no exc is wrapped.
        """
        with self._downloader_pool_with_retry(
            "download_ota_image_metafiles"
        ) as _mapper:
            try:
                for _fut in _mapper.ensure_tasks(
                    partial(
                        self._download_with_condition_at_thread, condition=condition
                    ),
                    _download_info,
                ):
                    yield _fut
            except TasksEnsureFailed as e:
                if e.cause:
                    raise e.cause from None
                raise

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
        """
        Raises:
            Last exception that interrupts the work pool, or TaskEnsureFailed if no exc is wrapped.
        """
        with self._downloader_pool_with_retry("download_ota_resources") as _mapper:
            try:
                for _fut in _mapper.ensure_tasks(
                    partial(
                        self._download_single_resource_at_thread,
                        resource_meta=resource_meta,
                    ),
                    self._iter_digests_with_shuffle(resources_to_download),
                ):
                    yield _fut
            except TasksEnsureFailed as e:
                if e.cause:
                    raise e.cause from None
                raise


RESOURCE_TABLE_DB_CONN = 3


class DownloadHelperForOTAImageV1(_BaseDownloadHelper):
    def _download_single_resource_at_thread(
        self, _digest: bytes, *, _rst_helper: PrepareResourceHelper, _base_url: str
    ) -> DownloadResult:
        downloader = self._downloader_mapper[threading.get_native_id()]

        resource, prepare_resource_gen = _rst_helper.prepare_resource_at_thread(_digest)
        _res = DownloadResult(download_size=resource.size)
        for _resource_dl_info in prepare_resource_gen:
            _blob_save_dst = _resource_dl_info.save_dst
            # reuse already downloaded blobs from previous OTA if presented
            if _blob_save_dst.is_file():
                if not file_sha256_2(_blob_save_dst).digest() == resource.digest:
                    # broken blob, cleanup and do the downloading again
                    _blob_save_dst.unlink(missing_ok=True)
                else:
                    continue  # re-use valid resource

            # compressed resource, try to decompress during downloading
            if _resource_dl_info.compression_alg:
                assert _resource_dl_info.compressed_origin_digest
                _this_res = downloader.download(
                    url=urljoin(_base_url, _resource_dl_info.digest.hex()),
                    dst=_blob_save_dst,
                    size=_resource_dl_info.compressed_origin_size,
                    compression_alg=_resource_dl_info.compression_alg,
                    digest=_resource_dl_info.compressed_origin_digest.hex(),
                )
            else:
                _this_res = downloader.download(
                    url=urljoin(_base_url, _resource_dl_info.digest.hex()),
                    dst=_blob_save_dst,
                    digest=_resource_dl_info.digest.hex(),
                )

            _res.retry_count += _this_res.retry_count
            _res.traffic_on_wire += _this_res.traffic_on_wire
        # after all the blobs are prepare, the `prepare_resource_at_thread` method will
        #   rebuild the requested origin blob.
        return _res

    def download_resources(
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

        with self._downloader_pool_with_retry(
            "download_ota_resources"
        ) as _mapper, _rst_orm_pool:
            for _fut in _mapper.ensure_tasks(
                partial(
                    self._download_single_resource_at_thread,
                    _rst_helper=_rst_helper,
                    _base_url=_base_url,
                ),
                self._iter_digests_with_shuffle(resources_to_download),
            ):
                yield _fut


class ResumeOTADownloadHelper:
    """
    NOTE: this helper class only works for OTA image v1.
    """

    def __init__(
        self,
        download_dir: Path,
        rst_helper: ResourceTableDBHelper,
        *,
        max_concurrent: int,
        db_conn_num: int = 1,  # serialize accessing
    ) -> None:
        self._download_dir = download_dir
        self._rst_orm_pool = rst_helper.get_orm_pool(db_conn_num)
        self._se = threading.Semaphore(max_concurrent)

    def _check_one_resource_at_thread(self, _fpath: Path, _digest: bytes):
        try:
            if (
                not self._rst_orm_pool.orm_check_entry_exist(digest=_digest)
                or file_sha256_2(_fpath).digest() != _digest
            ):
                remove_file(_fpath)
        except Exception:
            remove_file(_fpath)
        finally:
            self._se.release()

    def __call__(self) -> int:
        _count = 0
        with ThreadPoolExecutor() as pool, os.scandir(self._download_dir) as it:
            for entry in it:
                if (
                    not entry.is_file(follow_symlinks=False)
                    or len(entry.name) < SHA256DIGEST_HEX_LEN
                    or entry.name.startswith("tmp")
                ):
                    remove_file(entry.path)
                    continue

                entry_fname = entry.name
                # NOTE: for slice, a suffix will be appended to the filename.
                #       see ota-image-libs.v1.resource_table.db.PrepareResourceHelper
                #           for more details.
                _digest_hex = entry_fname[:SHA256DIGEST_HEX_LEN]
                try:
                    _digest = bytes.fromhex(_digest_hex)
                except Exception:
                    remove_file(entry.path)
                    continue

                self._se.acquire()
                _count += 1
                pool.submit(
                    self._check_one_resource_at_thread, Path(entry.path), _digest
                )
        return _count
