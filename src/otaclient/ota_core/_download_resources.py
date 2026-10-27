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
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Generator, Iterable
from urllib.parse import urljoin

from ota_image_libs._resource_filter import CompressFilter, SliceFilter
from ota_image_libs.common import tmp_fname
from ota_image_libs.v1.resource_table.db import (
    ResourceTableDBHelper,
    ResourceTableORMPool,
)
from ota_image_libs.v1.resource_table.utils import PrepareResourceHelper

from ota_metadata.legacy2.metadata import ResourceMeta
from otaclient_common import EMPTY_FILE_SHA256_BYTE, human_readable_size
from otaclient_common._logging import get_burst_suppressed_logger
from otaclient_common.download_info import DownloadInfo
from otaclient_common.downloader import (
    Downloader,
    DownloaderPool,
    DownloadPoolWatchdogFuncContext,
    DownloadResult,
    HashVerificationError,
    resumable_bytes,
)
from otaclient_common.retry_task_map import (
    TasksEnsureFailed,
    ThreadPoolExecutorWithRetry,
)

logger = logging.getLogger(__name__)
burst_suppressed_logger = get_burst_suppressed_logger(
    f"{__name__}.burst_suppressed_logger"
)

DEFAULT_SHUFFLE_BATCH_SIZE = 256

RESUMABLE_BLOB_MIN_SIZE = 1024**2  # 1MiB
"""An uncompressed blob of an OTA image v1 at least this large is downloaded through
`<digest>.part` in the download tmp dir, and a retry continues it with a ranged request
instead of fetching it from the start again. Below it, a download cut short costs too
little to matter."""
PARTIAL_SUFFIX = ".part"
"""What an unfinished download is called until its digest checks out: the file a
`DownloadInfo.resume` download or `download_resumable` continues."""


def download_resumable(
    downloader: Downloader, *, url: str, part: Path, digest: str, size: int
) -> DownloadResult:
    """Download a blob through <part>, continuing what an interrupted download left there.

    What is in <part> is continued with a ranged request when it is shorter than the blob
        (a prefix of it as far as its length tells; the digest, computed over the whole
        file, is what proves it). Bytes that hashed wrong are removed: they are not this
        blob, not even in part, and a retry must not build on them. On any other failure
        <part> is left as it is, for the retry to continue.
    """
    _have = resumable_bytes(part, size)
    if _have:
        burst_suppressed_logger.info(
            f"continuing {part.name}: {human_readable_size(_have)} of "
            f"{human_readable_size(size)} is here"
        )
    else:  # nothing to continue, or more than the blob has: not a prefix of it
        part.unlink(missing_ok=True)

    try:
        return downloader.download(
            url=url, dst=part, digest=digest, size=size, resume_from=_have
        )
    except HashVerificationError:
        part.unlink(missing_ok=True)
        raise


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
    def _downloader_pool_with_retry(
        self, thread_name_prefix: str, *, max_workers: int | None = None
    ):
        try:
            with ThreadPoolExecutorWithRetry(
                max_concurrent=self._max_concurrent,
                max_workers=max_workers or self._downloader_threads,
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

    @staticmethod
    def _download_one(
        downloader: Downloader, _download_info: DownloadInfo
    ) -> DownloadResult:
        if _download_info.resume:
            assert _download_info.digest and _download_info.original_size
            return download_resumable(
                downloader,
                url=_download_info.url,
                part=_download_info.dst,
                digest=_download_info.digest,
                size=_download_info.original_size,
            )
        return downloader.download(
            url=_download_info.url,
            dst=_download_info.dst,
            digest=_download_info.digest,
            size=_download_info.original_size,
            compression_alg=_download_info.compression_alg,
        )

    def _download_one_at_batch_thread(
        self, _download_info: DownloadInfo
    ) -> DownloadResult | None:
        """Download with a spare downloader instance, None if the pool has none."""
        try:
            _downloader = self._downloader_pool.get_instance()
        except ValueError:
            return None
        try:
            return self._download_one(_downloader, _download_info)
        finally:
            self._downloader_pool.release_instance()

    def _download_with_condition_at_thread(
        self, _to_download: list[DownloadInfo], condition: threading.Condition
    ) -> list[DownloadResult]:
        downloader = self._downloader_mapper[threading.get_native_id()]
        with condition:
            if len(_to_download) < 2:
                _res = [self._download_one(downloader, _info) for _info in _to_download]
                condition.notify()
                return _res

            # NOTE(20261025): the files of one batch (e.g., the file_table and the
            #   resource_table of OTA image v1, ~15MiB and ~10MiB) don't depend on
            #   each other, download them concurrently. Each download is one TCP
            #   stream, whose throughput is bounded by RTT and packet loss on cellular
            #   links, so downloading them one by one leaves most of the link idle.
            # NOTE: this worker downloads the first file, each of the others takes a
            #   spare downloader instance, one no worker thread holds: the metafiles
            #   pool has one worker thread (see download_meta_files), so no worker
            #   thread starts and asks for an instance while they are taken. A file
            #   that gets no spare instance is downloaded by this worker afterwards.
            with ThreadPoolExecutor(
                max_workers=len(_to_download) - 1,
                thread_name_prefix="download_ota_image_metafiles_batch",
            ) as _batch_pool:
                _others = [
                    _batch_pool.submit(self._download_one_at_batch_thread, _info)
                    for _info in _to_download[1:]
                ]
                _batch_res: list[DownloadResult | None] = [
                    self._download_one(downloader, _to_download[0])
                ]
                _batch_res += [_fut.result() for _fut in _others]

            if _no_spare := _batch_res.count(None):
                logger.info(
                    f"no spare downloader instance for {_no_spare} of the "
                    f"{len(_to_download)} files of the batch, download them one by one"
                )
            _res = [
                _r if _r is not None else self._download_one(downloader, _info)
                for _r, _info in zip(_batch_res, _to_download, strict=True)
            ]
            condition.notify()
        return _res

    def download_meta_files(
        self,
        _download_info: Generator[list[DownloadInfo]],
        condition: threading.Condition,
    ) -> Generator[Future[list[DownloadResult]]]:
        """
        The files of one batch must not depend on each other: they are downloaded
            concurrently. The producer yields the next batch only after the condition
            is notified, i.e., one batch at a time, so one worker thread is enough,
            leaving the other downloader instances to the files of the batch.

        Raises:
            Last exception that interrupts the work pool, or TaskEnsureFailed if no exc is wrapped.
        """
        with self._downloader_pool_with_retry(
            "download_ota_image_metafiles", max_workers=1
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


class _PartialDownloads:
    """The `<digest>.part` files of the download tmp dir, each written by one thread only.

    The same blob can be needed twice at the same time (a slice shared by two sliced
        resources); the one that comes second downloads it whole, straight to where it
        goes, and continues nothing.
    """

    def __init__(self, download_tmp_dir: Path) -> None:
        self._download_tmp_dir = download_tmp_dir
        self._lock = threading.Lock()
        self._in_use: set[bytes] = set()

    @contextlib.contextmanager
    def use(self, digest: bytes, size: int) -> Generator[Path | None]:
        """The `.part` to download the blob through, or None to download it whole."""
        if size < RESUMABLE_BLOB_MIN_SIZE:
            yield None
            return

        with self._lock:
            if digest in self._in_use:
                _part = None
            else:
                self._in_use.add(digest)
                _part = self._download_tmp_dir / f"{digest.hex()}{PARTIAL_SUFFIX}"
        try:
            yield _part
        finally:
            if _part is not None:
                with self._lock:
                    self._in_use.discard(digest)


class _SlicesGroup:
    """The slices of one sliced resource, downloaded as tasks of their own.

    Each slice is tried once. Once every slice has been tried, the resource is ready to
        be prepared: its slices are in the download dir, and PrepareResourceHelper
        downloads itself the ones whose download failed.
    """

    def __init__(
        self, resource: bytes, slices: Iterable[Path], ready: SimpleQueue[bytes]
    ) -> None:
        self.resource = resource
        self._not_tried = set(slices)
        self._ready = ready
        self._lock = threading.Lock()

    def tried(self, save_dst: Path) -> bool:
        with self._lock:
            return save_dst not in self._not_tried

    def slice_tried(self, save_dst: Path) -> None:
        """Called when the download of a slice ends, succeeded or not."""
        with self._lock:
            if save_dst not in self._not_tried:
                return
            self._not_tried.discard(save_dst)
            if self._not_tried:
                return
        self._ready.put_nowait(self.resource)


@dataclass
class SliceDownloadResult(DownloadResult):
    """The download of one slice of a resource, not a resource of its own."""


@dataclass(frozen=True)
class _SliceToDownload:
    digest: bytes
    size: int
    save_dst: Path
    group: _SlicesGroup


class DownloadHelperForOTAImageV1(_BaseDownloadHelper):
    RESOURCE_TABLE_DB_CONN = 3

    SLICES_WAIT_POLL_INTERVAL = 1  # seconds
    """How often the dispatcher, waiting at the end for slices in flight, checks for stop."""

    @staticmethod
    def _slices_to_download(
        _digest: bytes, rst_orm_pool: ResourceTableORMPool, download_tmp_dir: Path
    ) -> list[tuple[bytes, int, Path]]:
        """The (digest, size, save_dst) of each slice of the resource still to download.

        Empty if the resource is not sliced, or has nothing left to download. A resource
            is sliced directly, or its compressed blob is. The save_dst is where
            PrepareResourceHelper._prepare_sliced_resources keeps the slices of the sliced
            entry, using one already there with the expected digest instead of downloading
            it again; a compressed blob already joined from its slices is kept as
            <download_tmp_dir>/<blob digest> by _prepare_compressed_resources. A slice, or
            a joined blob, that an interrupted OTA left there was checked by
            ResumeOTADownloadHelper at the start of this one.

        NOTE: this follows the internals of ota-image-libs (the filters of an entry, and
            where PrepareResourceHelper keeps the blobs it prepares a resource from); an API
            there giving the slices to prefetch for a digest would be cleaner.
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

        _slices: dict[Path, tuple[bytes, int]] = {}
        for _slice_rsid in _entry.filter_applied.list_resource_id():
            _slice = rst_orm_pool.orm_select_entry(resource_id=_slice_rsid)
            if _slice is None:
                return []  # let PrepareResourceHelper report the broken image
            _save_dst = download_tmp_dir / f"{_slice.digest.hex()}_{_entry.resource_id}"
            # NOTE: a slice repeats in a resource with repeating contents (e.g., zeros)
            if _save_dst not in _slices and not _save_dst.is_file():
                _slices[_save_dst] = (_slice.digest, _slice.size)
        return [
            (_slice_digest, _slice_size, _save_dst)
            for _save_dst, (_slice_digest, _slice_size) in _slices.items()
        ]

    def _iter_download_tasks(
        self,
        resources_to_download: Iterable[bytes],
        *,
        rst_orm_pool: ResourceTableORMPool,
        download_tmp_dir: Path,
        stop: threading.Event,
    ) -> Generator[bytes | _SliceToDownload]:
        """Resources in shuffled order, with the slices of a sliced resource as tasks.

        A sliced resource is a large file, preparing it downloads its slices one after
            another in one thread, i.e., one TCP stream, whose throughput on a lossy
            cellular link is a fraction of the link's. Its slices are dispatched as tasks
            of their own instead, spreading over all the downloading threads, and the
            resource itself, left with joining its slices, as soon as all of them have
            been tried. The ones still waiting for slices in flight once everything else
            is dispatched are waited for here, in the dispatcher, until <stop> is set.

        NOTE: a stalled download is the pool's watchdog's business: the pool is shut down,
            its queued tasks dropped without being run, and the caller sets <stop>.
        """
        _ready: SimpleQueue[bytes] = SimpleQueue()
        _waiting: dict[bytes, _SlicesGroup] = {}
        _sliced_count, _slices_count = 0, 0
        for _digest in self._iter_digests_with_shuffle(resources_to_download):
            try:
                _slices = self._slices_to_download(
                    _digest, rst_orm_pool, download_tmp_dir
                )
            except Exception as e:  # leave it to PrepareResourceHelper
                logger.warning(
                    f"failed to look up the slices of {_digest.hex()}: {e!r}"
                )
                _slices = []

            if _slices:
                _group = _waiting[_digest] = _SlicesGroup(
                    _digest, (_save_dst for _, _, _save_dst in _slices), _ready
                )
                _sliced_count += 1
                _slices_count += len(_slices)
                for _slice_digest, _slice_size, _save_dst in _slices:
                    yield _SliceToDownload(
                        _slice_digest, _slice_size, _save_dst, _group
                    )
            else:
                yield _digest

            with contextlib.suppress(Empty):
                while True:
                    _ready_digest = _ready.get_nowait()
                    del _waiting[_ready_digest]
                    yield _ready_digest

        if _sliced_count:
            logger.info(
                f"dispatched {_slices_count} slices of {_sliced_count} sliced resources "
                f"as download tasks, {len(_waiting)} of them wait for slices in flight"
            )
        while _waiting:
            try:
                _ready_digest = _ready.get(timeout=self.SLICES_WAIT_POLL_INTERVAL)
            except Empty:
                if stop.is_set():
                    return
                continue
            del _waiting[_ready_digest]
            yield _ready_digest

    @staticmethod
    def _download_blob(
        downloader: Downloader,
        *,
        url: str,
        digest: bytes,
        size: int,
        save_dst: Path,
        _partials: _PartialDownloads,
    ) -> DownloadResult:
        """Download an uncompressed blob to <save_dst>, through `<digest>.part` in the
        download tmp dir when it is large enough to be worth continuing: what an
        interrupted download of it left there is continued with a ranged request by
        whichever task comes to the blob next in this download run (the retry of the
        task, or the resource's own download of a slice whose slice task was cut), so
        a link that drops every few minutes costs the time it is down rather than the
        blob again. The `.part` lives as long as this run: the next OTA's
        ResumeOTADownloadHelper drops what it cannot verify, a `.part` included."""
        with _partials.use(digest, size) as _part:
            if _part is None:
                return downloader.download(
                    url=url, dst=save_dst, digest=digest.hex(), size=size
                )
            _res = download_resumable(
                downloader, url=url, part=_part, digest=digest.hex(), size=size
            )
            os.replace(_part, save_dst)
            return _res

    def _download_slice_at_thread(
        self,
        _slice: _SliceToDownload,
        *,
        _base_url: str,
        _partials: _PartialDownloads,
    ) -> DownloadResult:
        # NOTE: a slice is tried once, a retry of a failed one does nothing: once every
        #   slice of the resource has been tried, the resource is prepared, and
        #   PrepareResourceHelper downloads the failed one itself, continuing what
        #   this try left in the `.part`.
        if _slice.group.tried(_slice.save_dst):
            return SliceDownloadResult()

        downloader = self._downloader_mapper[threading.get_native_id()]
        # NOTE: a small slice is downloaded straight into this file, so it has a name of
        #   its own for each attempt, with the prefix that makes ResumeOTADownloadHelper
        #   clean up one left behind. A large one arrives in its `.part` and is only
        #   moved through here once it is whole and verified.
        _tmp_dst = _slice.save_dst.parent / tmp_fname(_slice.save_dst.name)
        try:
            _this_res = self._download_blob(
                downloader,
                url=urljoin(_base_url, _slice.digest.hex()),
                digest=_slice.digest,
                size=_slice.size,
                save_dst=_tmp_dst,
                _partials=_partials,
            )
            os.replace(_tmp_dst, _slice.save_dst)
        finally:
            _tmp_dst.unlink(missing_ok=True)
            _slice.group.slice_tried(_slice.save_dst)
        return SliceDownloadResult(
            retry_count=_this_res.retry_count,
            traffic_on_wire=_this_res.traffic_on_wire,
        )

    def _download_task_at_thread(
        self,
        _task: bytes | _SliceToDownload,
        *,
        _rst_helper: PrepareResourceHelper,
        _base_url: str,
        _partials: _PartialDownloads,
    ) -> DownloadResult:
        if isinstance(_task, _SliceToDownload):
            return self._download_slice_at_thread(
                _task, _base_url=_base_url, _partials=_partials
            )
        return self._download_single_resource_at_thread(
            _task, _rst_helper=_rst_helper, _base_url=_base_url, _partials=_partials
        )

    def _download_single_resource_at_thread(
        self,
        _digest: bytes,
        *,
        _rst_helper: PrepareResourceHelper,
        _base_url: str,
        _partials: _PartialDownloads,
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
                # NOTE: the compressed blob above is decompressed as it arrives, so
                #   there is no byte offset in it to continue from (and its cache key
                #   at otaproxy and in an external cache stays as it is); a large one
                #   is sliced, and its slices are uncompressed blobs, continued here.
                _this_res = self._download_blob(
                    downloader,
                    url=urljoin(_base_url, _resource_dl_info.digest.hex()),
                    digest=_resource_dl_info.digest,
                    size=_resource_dl_info.size,
                    save_dst=_blob_save_dst,
                    _partials=_partials,
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

        _stop_dispatching = threading.Event()
        with (
            self._downloader_pool_with_retry("download_ota_resources") as _mapper,
            _rst_orm_pool,
        ):
            try:
                for _fut in _mapper.ensure_tasks(
                    partial(
                        self._download_task_at_thread,
                        _rst_helper=_rst_helper,
                        _base_url=_base_url,
                        _partials=_PartialDownloads(download_tmp_dir),
                    ),
                    self._iter_download_tasks(
                        resources_to_download,
                        rst_orm_pool=_rst_orm_pool,
                        download_tmp_dir=download_tmp_dir,
                        stop=_stop_dispatching,
                    ),
                ):
                    yield _fut
            finally:
                _stop_dispatching.set()
