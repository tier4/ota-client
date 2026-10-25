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
import logging
import os
import random
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Generator, Iterable, Union
from urllib.parse import urljoin

from ota_image_libs._resource_filter import CompressFilter, SliceFilter
from ota_image_libs.common import tmp_fname
from ota_image_libs.v1.resource_table.db import (
    ResourceTableDBHelper,
    ResourceTableORMPool,
)
from ota_image_libs.v1.resource_table.utils import PrepareResourceHelper

from ota_metadata.legacy2.metadata import ResourceMeta
from otaclient_common import EMPTY_FILE_SHA256_BYTE
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

logger = logging.getLogger(__name__)

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


class _SlicesGroup:
    """The slices of one sliced resource, downloaded as tasks of their own.

    Once every slice has been tried, the resource is ready to be prepared: its slices are
        in the download dir, and PrepareResourceHelper downloads itself the ones whose
        download failed.
    """

    def __init__(
        self, resource: bytes, slices: Iterable[Path], ready: SimpleQueue[bytes]
    ) -> None:
        self.resource = resource
        self._not_tried = set(slices)
        self._ready = ready
        self._lock = threading.Lock()

    @property
    def all_tried(self) -> bool:
        with self._lock:
            return not self._not_tried

    def slice_tried(self, save_dst: Path) -> None:
        """Called each time the download of a slice ends, succeeded or not.

        A slice counts once: its retry ending does not stand for another slice.
        """
        with self._lock:
            if save_dst not in self._not_tried:
                return
            self._not_tried.discard(save_dst)
            if self._not_tried:
                return
        self._ready.put_nowait(self.resource)

    def hand_over(self) -> None:
        """Leave the slices not tried yet to PrepareResourceHelper."""
        with self._lock:
            self._not_tried.clear()


@dataclass(frozen=True)
class _SliceToDownload:
    digest: bytes
    save_dst: Path
    group: _SlicesGroup


class DownloadHelperForOTAImageV1(_BaseDownloadHelper):
    RESOURCE_TABLE_DB_CONN = 3

    SLICES_WAIT_TIMEOUT = 60 * 60  # seconds
    """How long the dispatcher waits for the slices still in flight at the end.

    A safety net only: every slice task reports when its download ends, succeeded or
        not, so as long as the pool runs, this waits for downloads in flight, however
        slow the link is. It is the dispatcher that waits, not a downloading thread.
    """

    @staticmethod
    def _slices_to_download(
        _digest: bytes, rst_orm_pool: ResourceTableORMPool, download_tmp_dir: Path
    ) -> list[tuple[bytes, Path]]:
        """The (digest, save_dst) of each slice of the resource still to download.

        Empty if the resource is not sliced, or has nothing left to download. A resource
            is sliced directly, or its compressed blob is. The save_dst is where
            PrepareResourceHelper._prepare_sliced_resources keeps the slices of the sliced
            entry, using one already there with the expected digest instead of downloading
            it again. A slice, or a compressed blob already joined from its slices, that an
            interrupted OTA left there was checked by ResumeOTADownloadHelper at the start
            of this one.
        """
        _entry = rst_orm_pool.orm_select_entry(digest=_digest)
        if _entry is not None and isinstance(_entry.filter_applied, CompressFilter):
            _entry = rst_orm_pool.orm_select_entry(
                resource_id=_entry.filter_applied.list_resource_id()
            )
            if (
                _entry is not None
                and (download_tmp_dir / _entry.digest.hex()).is_file()
            ):
                return []
        if _entry is None or not isinstance(_entry.filter_applied, SliceFilter):
            return []

        _slices = []
        for _slice_rsid in _entry.filter_applied.list_resource_id():
            _slice = rst_orm_pool.orm_select_entry(resource_id=_slice_rsid)
            if _slice is None:
                return []  # let PrepareResourceHelper report the broken image
            _save_dst = download_tmp_dir / f"{_slice.digest.hex()}_{_entry.resource_id}"
            if not _save_dst.is_file():
                _slices.append((_slice.digest, _save_dst))
        return _slices

    def _iter_download_tasks(
        self,
        resources_to_download: Iterable[bytes],
        *,
        rst_orm_pool: ResourceTableORMPool,
        download_tmp_dir: Path,
    ) -> Generator[Union[bytes, _SliceToDownload]]:
        """Resources in shuffled order, with the slices of a sliced resource as tasks.

        A sliced resource is a large file, preparing it downloads its slices one after
            another in one thread, i.e., one TCP stream, whose throughput on a lossy
            cellular link is a fraction of the link's. Its slices are dispatched as tasks
            of their own instead, spreading over all the downloading threads, and the
            resource itself, left with joining its slices, as soon as all of them have
            been tried. The ones still waiting for slices in flight once everything else
            is dispatched are waited for here, in the dispatcher.
        """
        _ready: SimpleQueue[bytes] = SimpleQueue()
        _waiting: dict[bytes, _SlicesGroup] = {}
        for _digest in self._iter_digests_with_shuffle(resources_to_download):
            if _slices := self._slices_to_download(
                _digest, rst_orm_pool, download_tmp_dir
            ):
                _group = _waiting[_digest] = _SlicesGroup(
                    _digest, (_save_dst for _, _save_dst in _slices), _ready
                )
                for _slice_digest, _save_dst in _slices:
                    yield _SliceToDownload(_slice_digest, _save_dst, _group)
            else:
                yield _digest

            with contextlib.suppress(Empty):
                while True:
                    _ready_digest = _ready.get_nowait()
                    del _waiting[_ready_digest]
                    yield _ready_digest

        while _waiting:
            try:
                _ready_digest = _ready.get(timeout=self.SLICES_WAIT_TIMEOUT)
            except Empty:
                logger.warning(
                    f"{len(_waiting)} sliced resources still wait for their slices "
                    f"after {self.SLICES_WAIT_TIMEOUT}s, dispatch them anyway"
                )
                for _ready_digest, _group in _waiting.items():
                    _group.hand_over()
                    yield _ready_digest
                return
            del _waiting[_ready_digest]
            yield _ready_digest

    def _download_slice_at_thread(
        self, _slice: _SliceToDownload, *, _base_url: str
    ) -> DownloadResult:
        # NOTE: once every slice of the resource has been tried, the resource is being
        #   prepared, and PrepareResourceHelper downloads a slice that failed itself.
        if _slice.group.all_tried:
            return DownloadResult(resource_slice=True)

        downloader = self._downloader_mapper[threading.get_native_id()]
        # NOTE: a name of its own for each attempt, as two resources can share a sliced
        #   blob; the prefix makes ResumeOTADownloadHelper clean up one left behind.
        _tmp_dst = _slice.save_dst.parent / tmp_fname(_slice.save_dst.name)
        try:
            _this_res = downloader.download(
                url=urljoin(_base_url, _slice.digest.hex()),
                dst=_tmp_dst,
                digest=_slice.digest.hex(),
            )
            os.replace(_tmp_dst, _slice.save_dst)
        finally:
            _tmp_dst.unlink(missing_ok=True)
            _slice.group.slice_tried(_slice.save_dst)
        return DownloadResult(
            retry_count=_this_res.retry_count,
            traffic_on_wire=_this_res.traffic_on_wire,
            resource_slice=True,
        )

    def _download_task_at_thread(
        self,
        _task: Union[bytes, _SliceToDownload],
        *,
        _rst_helper: PrepareResourceHelper,
        _base_url: str,
    ) -> DownloadResult:
        if isinstance(_task, _SliceToDownload):
            return self._download_slice_at_thread(_task, _base_url=_base_url)
        return self._download_single_resource_at_thread(
            _task, _rst_helper=_rst_helper, _base_url=_base_url
        )

    def _download_single_resource_at_thread(
        self, _digest: bytes, *, _rst_helper: PrepareResourceHelper, _base_url: str
    ) -> DownloadResult:
        downloader = self._downloader_mapper[threading.get_native_id()]

        resource, prepare_resource_gen = _rst_helper.prepare_resource_at_thread(_digest)
        _res = DownloadResult(download_size=resource.size)
        for _resource_dl_info in prepare_resource_gen:
            _blob_save_dst = _resource_dl_info.save_dst
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
        _rst_orm_pool = resource_db_helper.get_orm_pool(self.RESOURCE_TABLE_DB_CONN)
        _rst_helper = PrepareResourceHelper(
            _rst_orm_pool,
            resource_dir=resource_dir,
            download_tmp_dir=download_tmp_dir,
        )

        with (
            self._downloader_pool_with_retry("download_ota_resources") as _mapper,
            _rst_orm_pool,
        ):
            for _fut in _mapper.ensure_tasks(
                partial(
                    self._download_task_at_thread,
                    _rst_helper=_rst_helper,
                    _base_url=_base_url,
                ),
                self._iter_download_tasks(
                    resources_to_download,
                    rst_orm_pool=_rst_orm_pool,
                    download_tmp_dir=download_tmp_dir,
                ),
            ):
                yield _fut
