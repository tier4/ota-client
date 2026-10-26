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
"""Tests for downloading the slices of sliced resources as tasks of their own."""

from __future__ import annotations

import hashlib
import itertools
import os
import threading
from collections import Counter
from concurrent.futures import Future
from contextlib import closing
from pathlib import Path
from queue import Queue, SimpleQueue
from types import SimpleNamespace

import pytest
import zstandard
from ota_image_libs._resource_filter import CompressFilter, SliceFilter
from ota_image_libs.v1.resource_table.db import (
    ResourceTableDBHelper,
    ResourceTableORM,
)
from ota_image_libs.v1.resource_table.schema import ResourceTableManifest
from pytest_mock import MockerFixture

from otaclient.ota_core._download_resources import (
    DownloadHelperForOTAImageV1,
    SliceDownloadResult,
    _SlicesGroup,
    _SliceToDownload,
)
from otaclient.ota_core._update_libs import download_resources_handler
from otaclient_common.downloader import Downloader, DownloaderPool, DownloadResult

BASE_URL = "https://example.com/ota/blobs/sha256/"


def _entry(resource_id: int, digest: bytes, filter_applied=None):
    return SimpleNamespace(
        resource_id=resource_id, digest=digest, size=1, filter_applied=filter_applied
    )


class _FakeResourceTable:
    """Answers orm_select_entry(digest=...) / orm_select_entry(resource_id=...)."""

    def __init__(self, *entries) -> None:
        self._by_digest = {_e.digest: _e for _e in entries}
        self._by_id = {_e.resource_id: _e for _e in entries}

    def orm_select_entry(self, *, digest=None, resource_id=None):
        if digest is not None:
            return self._by_digest.get(digest)
        return self._by_id.get(resource_id)


PLAIN = _entry(1, b"plain")
SLICE_A, SLICE_B = _entry(10, b"slice-a"), _entry(11, b"slice-b")
SLICED = _entry(2, b"sliced", SliceFilter(slices=[10, 11]))
COMPRESSED_SLICED = _entry(3, b"compressed-sliced", CompressFilter(4, "zst"))
COMPRESSED_BLOB = _entry(4, b"compressed-blob", SliceFilter(slices=[11]))
COMPRESSED_PLAIN = _entry(5, b"compressed-plain", CompressFilter(6, "zst"))
COMPRESSED_PLAIN_BLOB = _entry(6, b"compressed-plain-blob")
BROKEN_SLICED = _entry(7, b"broken-sliced", SliceFilter(slices=[10, 99]))
BROKEN_COMPRESSED = _entry(8, b"broken-compressed", CompressFilter(98, "zst"))
REPEATING = _entry(9, b"repeating", SliceFilter(slices=[10, 11, 10]))
RESOURCE_TABLE = _FakeResourceTable(
    PLAIN,
    SLICE_A,
    SLICE_B,
    SLICED,
    COMPRESSED_SLICED,
    COMPRESSED_BLOB,
    COMPRESSED_PLAIN,
    COMPRESSED_PLAIN_BLOB,
    BROKEN_SLICED,
    BROKEN_COMPRESSED,
    REPEATING,
)


@pytest.fixture
def helper(mocker: MockerFixture) -> DownloadHelperForOTAImageV1:
    pool = mocker.MagicMock(spec=DownloaderPool)
    pool.instance_num = 4
    return DownloadHelperForOTAImageV1(
        downloader_pool=pool, max_concurrent=4, download_inactive_timeout=10
    )


@pytest.fixture
def no_shuffle(mocker: MockerFixture) -> None:
    mocker.patch("otaclient.ota_core._download_resources.random.shuffle")


class TestSlicesToDownload:
    @pytest.mark.parametrize(
        "digest, expected",
        (
            pytest.param(PLAIN.digest, [], id="plain"),
            pytest.param(COMPRESSED_PLAIN.digest, [], id="compressed_not_sliced"),
            pytest.param(b"not-in-table", [], id="unknown"),
            # left to PrepareResourceHelper, which reports the broken image
            pytest.param(BROKEN_SLICED.digest, [], id="slice_missing"),
            pytest.param(BROKEN_COMPRESSED.digest, [], id="compressed_blob_missing"),
            pytest.param(
                SLICED.digest,
                [
                    (SLICE_A.digest, f"{SLICE_A.digest.hex()}_{SLICED.resource_id}"),
                    (SLICE_B.digest, f"{SLICE_B.digest.hex()}_{SLICED.resource_id}"),
                ],
                id="sliced",
            ),
            pytest.param(
                COMPRESSED_SLICED.digest,
                # the slices are of the compressed blob
                [
                    (
                        SLICE_B.digest,
                        f"{SLICE_B.digest.hex()}_{COMPRESSED_BLOB.resource_id}",
                    )
                ],
                id="compressed_then_sliced",
            ),
            pytest.param(
                REPEATING.digest,
                # a slice repeating in the resource is downloaded once
                [
                    (SLICE_A.digest, f"{SLICE_A.digest.hex()}_{REPEATING.resource_id}"),
                    (SLICE_B.digest, f"{SLICE_B.digest.hex()}_{REPEATING.resource_id}"),
                ],
                id="a_slice_repeats",
            ),
        ),
    )
    def test_slices_to_download(self, digest: bytes, expected, tmp_path: Path):
        res = DownloadHelperForOTAImageV1._slices_to_download(
            digest,
            RESOURCE_TABLE,  # type: ignore[arg-type]
            tmp_path,
        )
        assert res == [(_d, tmp_path / _name) for _d, _name in expected]

    def test_a_slice_an_interrupted_ota_left_is_not_downloaded_again(
        self, tmp_path: Path
    ):
        (tmp_path / f"{SLICE_A.digest.hex()}_{SLICED.resource_id}").write_bytes(b"a")

        res = DownloadHelperForOTAImageV1._slices_to_download(
            SLICED.digest,
            RESOURCE_TABLE,  # type: ignore[arg-type]
            tmp_path,
        )

        assert res == [
            (SLICE_B.digest, tmp_path / f"{SLICE_B.digest.hex()}_{SLICED.resource_id}")
        ]

    def test_a_compressed_blob_already_joined_needs_no_slice(self, tmp_path: Path):
        (tmp_path / COMPRESSED_BLOB.digest.hex()).write_bytes(b"joined")

        res = DownloadHelperForOTAImageV1._slices_to_download(
            COMPRESSED_SLICED.digest,
            RESOURCE_TABLE,  # type: ignore[arg-type]
            tmp_path,
        )

        assert res == []


class TestSlicesGroup:
    def test_a_slice_counts_once_however_often_it_is_retried(self):
        ready: SimpleQueue[bytes] = SimpleQueue()
        group = _SlicesGroup(b"resource", [Path("a"), Path("b")], ready)

        group.slice_tried(Path("a"))
        group.slice_tried(Path("a"))
        assert group.tried(Path("a")) and not group.tried(Path("b"))
        assert ready.empty()

        group.slice_tried(Path("b"))
        assert group.tried(Path("b")) and ready.get_nowait() == b"resource"


class TestIterDownloadTasks:
    def _iter(
        self,
        helper: DownloadHelperForOTAImageV1,
        digests,
        tmp_path: Path,
        stop: threading.Event | None = None,
        resource_table=RESOURCE_TABLE,
    ):
        return helper._iter_download_tasks(
            digests,
            rst_orm_pool=resource_table,
            download_tmp_dir=tmp_path,
            stop=stop or threading.Event(),
        )

    def test_a_sliced_resource_follows_once_its_slices_have_been_tried(
        self, helper: DownloadHelperForOTAImageV1, tmp_path: Path, no_shuffle
    ):
        tasks = self._iter(helper, [SLICED.digest, PLAIN.digest], tmp_path)
        slice_a, slice_b = next(tasks), next(tasks)
        assert isinstance(slice_a, _SliceToDownload)
        assert isinstance(slice_b, _SliceToDownload)
        assert {slice_a.digest, slice_b.digest} == {SLICE_A.digest, SLICE_B.digest}
        assert slice_a.group is slice_b.group

        slice_a.group.slice_tried(slice_a.save_dst)
        slice_b.group.slice_tried(slice_b.save_dst)

        # dispatched as soon as it is ready, not after every other resource
        assert next(tasks) == SLICED.digest
        assert list(tasks) == [PLAIN.digest]

    def test_one_with_slices_in_flight_is_waited_for_at_the_end(
        self, helper: DownloadHelperForOTAImageV1, tmp_path: Path, no_shuffle
    ):
        tasks = self._iter(helper, [SLICED.digest, PLAIN.digest], tmp_path)
        slices = [next(tasks), next(tasks)]
        assert next(tasks) == PLAIN.digest

        slices_tried = threading.Event()

        def _slices_end():
            slices_tried.set()
            for _s in slices:
                _s.group.slice_tried(_s.save_dst)

        _timer = threading.Timer(0.5, _slices_end)
        _timer.start()
        try:
            assert next(tasks) == SLICED.digest
            # not before every slice of it has been tried
            assert slices_tried.is_set()
            assert list(tasks) == []
        finally:
            _timer.join(timeout=5)
        assert not _timer.is_alive()

    def test_the_wait_ends_when_dispatching_stops(
        self,
        helper: DownloadHelperForOTAImageV1,
        tmp_path: Path,
        no_shuffle,
        mocker: MockerFixture,
    ):
        """E.g., the pool's watchdog shut it down, dropping the queued slice tasks."""
        mocker.patch.object(
            DownloadHelperForOTAImageV1, "SLICES_WAIT_POLL_INTERVAL", 0.05
        )
        stop = threading.Event()
        tasks = self._iter(helper, [SLICED.digest], tmp_path, stop=stop)
        next(tasks), next(tasks)  # the slices, never run

        _timer = threading.Timer(0.2, stop.set)
        _timer.start()
        try:
            assert list(tasks) == []
        finally:
            _timer.join(timeout=5)

    def test_a_failed_slices_lookup_leaves_the_resource_to_prepare_resource_helper(
        self,
        helper: DownloadHelperForOTAImageV1,
        tmp_path: Path,
        no_shuffle,
        mocker: MockerFixture,
    ):
        broken_table = mocker.MagicMock()
        broken_table.orm_select_entry.side_effect = OSError("database disk image")

        tasks = self._iter(
            helper, [SLICED.digest, PLAIN.digest], tmp_path, resource_table=broken_table
        )

        assert list(tasks) == [SLICED.digest, PLAIN.digest]

    def test_a_resource_whose_slices_are_all_there_is_dispatched_as_it_is(
        self, helper: DownloadHelperForOTAImageV1, tmp_path: Path, no_shuffle
    ):
        for _slice in (SLICE_A, SLICE_B):
            (tmp_path / f"{_slice.digest.hex()}_{SLICED.resource_id}").write_bytes(b"x")

        assert list(self._iter(helper, [SLICED.digest], tmp_path)) == [SLICED.digest]


class TestDownloadSliceAtThread:
    @pytest.fixture
    def worker_downloader(self, helper: DownloadHelperForOTAImageV1, mocker):
        downloader = mocker.create_autospec(Downloader, instance=True)
        helper._downloader_mapper[threading.get_native_id()] = downloader
        return downloader

    @staticmethod
    def _slice(tmp_path: Path, *other_slices: Path) -> _SliceToDownload:
        save_dst = tmp_path / f"{b'slice-a'.hex()}_{SLICED.resource_id}"
        group = _SlicesGroup(SLICED.digest, [save_dst, *other_slices], SimpleQueue())
        return _SliceToDownload(b"slice-a", save_dst, group)

    @staticmethod
    def _run(helper: DownloadHelperForOTAImageV1, _slice: _SliceToDownload):
        return helper._download_task_at_thread(
            _slice,
            _rst_helper=None,  # type: ignore[arg-type]
            _base_url=BASE_URL,
        )

    def test_a_slice_lands_through_a_tmp_file_where_prepare_resource_helper_looks(
        self, helper: DownloadHelperForOTAImageV1, worker_downloader, tmp_path: Path
    ):
        _slice = self._slice(tmp_path)

        def _download(*, url: str, dst: Path, digest: str, **_) -> DownloadResult:
            # not written where PrepareResourceHelper looks until it hashed right
            assert dst.parent == tmp_path and dst.name.startswith("tmp")
            dst.write_bytes(b"slice")
            return DownloadResult(retry_count=1, traffic_on_wire=5)

        worker_downloader.download.side_effect = _download

        res = self._run(helper, _slice)

        worker_downloader.download.assert_called_once()
        _kwargs = worker_downloader.download.call_args.kwargs
        assert _kwargs["url"] == BASE_URL + b"slice-a".hex()
        assert _kwargs["digest"] == b"slice-a".hex()
        assert _slice.save_dst.read_bytes() == b"slice"
        assert list(tmp_path.iterdir()) == [_slice.save_dst], "no tmp file left"
        assert res == SliceDownloadResult(retry_count=1, traffic_on_wire=5)
        assert _slice.group.tried(_slice.save_dst)

    def test_a_failed_download_leaves_nothing_and_counts_the_slice_tried(
        self, helper: DownloadHelperForOTAImageV1, worker_downloader, tmp_path: Path
    ):
        def _download(*, dst: Path, **_) -> DownloadResult:
            dst.write_bytes(b"half a slice")
            raise ValueError("broken stream")

        worker_downloader.download.side_effect = _download
        _slice = self._slice(tmp_path, tmp_path / "another-slice")

        with pytest.raises(ValueError, match="broken stream"):
            self._run(helper, _slice)

        assert list(tmp_path.iterdir()) == []
        assert _slice.group.tried(_slice.save_dst)
        # the other slice is still to be tried
        assert not _slice.group.tried(tmp_path / "another-slice")

    def test_a_retry_of_a_failed_slice_is_left_to_the_resource(
        self, helper: DownloadHelperForOTAImageV1, worker_downloader, tmp_path: Path
    ):
        """Even while other slices of the resource are still to be tried."""
        worker_downloader.download.side_effect = ValueError("broken stream")
        _slice = self._slice(tmp_path, tmp_path / "another-slice")
        with pytest.raises(ValueError):
            self._run(helper, _slice)

        res = self._run(helper, _slice)  # what the executor's retry runs

        assert worker_downloader.download.call_count == 1
        assert res == SliceDownloadResult()


def test_slices_are_not_counted_as_downloaded_files(mocker: MockerFixture):
    def _done(res: DownloadResult) -> Future:
        _fut = Future()
        _fut.set_result(res)
        return _fut

    report_que = Queue()
    download_resources_handler(
        iter(
            [
                _done(DownloadResult(download_size=100, traffic_on_wire=10)),
                _done(SliceDownloadResult(traffic_on_wire=40)),
                _done(SliceDownloadResult(traffic_on_wire=40)),
                _done(DownloadResult(download_size=200, traffic_on_wire=1)),
            ]
        ),
        metrics=mocker.MagicMock(),
        status_report_queue=report_que,
        session_id="session",
    )

    reports = [report_que.get_nowait().payload for _ in range(report_que.qsize())]
    assert sum(_r.processed_file_num for _r in reports) == 2
    assert sum(_r.processed_file_size for _r in reports) == 300
    assert sum(_r.downloaded_bytes for _r in reports) == 91


#
# ------ with a real resource table and PrepareResourceHelper ------ #
#


class _BlobStorage:
    """The blobs of an OTA image, downloaded by digest; counts the requests."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.requests: Counter[str] = Counter()
        self.fail_first: set[str] = set()
        """The blobs whose first request fails."""
        self._lock = threading.Lock()

    def add(self, data: bytes) -> bytes:
        _digest = hashlib.sha256(data).digest()
        self.blobs[_digest.hex()] = data
        return _digest

    def download(self, url, dst, *, digest=None, **_) -> DownloadResult:
        _name = url.rsplit("/", 1)[-1]
        with self._lock:
            self.requests[_name] += 1
            if _name in self.fail_first and self.requests[_name] == 1:
                raise ValueError("broken stream")
        _data = self.blobs[_name]
        assert digest == hashlib.sha256(_data).hexdigest()
        Path(dst).write_bytes(_data)
        return DownloadResult(download_size=len(_data), traffic_on_wire=len(_data))


@pytest.fixture
def image(tmp_path: Path):
    """A resource table with a plain, a sliced, and a compressed-then-sliced resource."""
    storage, entries, _rsid = _BlobStorage(), [], itertools.count(1)

    def _add(digest: bytes, size: int, filter_applied=None) -> ResourceTableManifest:
        _e = ResourceTableManifest(
            resource_id=next(_rsid),
            digest=digest,
            size=size,
            filter_applied=filter_applied,
        )
        entries.append(_e)
        return _e

    plain = os.urandom(100)
    _plain = _add(storage.add(plain), len(plain))

    chunks = [os.urandom(64) for _ in range(3)]
    _chunks = [_add(storage.add(_c), len(_c)) for _c in chunks]
    sliced = b"".join(chunks)
    _sliced = _add(
        hashlib.sha256(sliced).digest(),
        len(sliced),
        SliceFilter(slices=[_c.resource_id for _c in _chunks]),
    )

    origin = b"a compressible line\n" * 200
    blob = zstandard.ZstdCompressor().compress(origin)
    _blob_slices = [
        _add(storage.add(_part), len(_part))
        for _part in (blob[: len(blob) // 2], blob[len(blob) // 2 :])
    ]
    _blob = _add(
        hashlib.sha256(blob).digest(),
        len(blob),
        SliceFilter(slices=[_s.resource_id for _s in _blob_slices]),
    )
    _compressed = _add(
        hashlib.sha256(origin).digest(),
        len(origin),
        CompressFilter(_blob.resource_id, "zstd"),
    )

    db_helper = ResourceTableDBHelper(tmp_path / "resource_table.sqlite3")
    db_helper.bootstrap_db()
    with closing(db_helper.connect_rstable_db()) as _conn:
        ResourceTableORM(_conn).orm_insert_entries(entries)
        _conn.commit()

    return SimpleNamespace(
        storage=storage,
        db_helper=db_helper,
        resources={
            _plain.digest: plain,
            _sliced.digest: sliced,
            _compressed.digest: origin,
        },
        sliced=_sliced,
        a_slice_of_sliced=(_chunks[0], chunks[0]),
        compressed=_compressed,
        compressed_blob=(_blob, blob),
        compressed_blob_slices=_blob_slices,
    )


class TestDownloadResources:
    @pytest.fixture
    def run(self, image, tmp_path: Path, mocker: MockerFixture):
        pool = mocker.MagicMock(spec=DownloaderPool)
        pool.instance_num = 3
        pool.get_instance.return_value = image.storage
        helper = DownloadHelperForOTAImageV1(
            downloader_pool=pool, max_concurrent=8, download_inactive_timeout=60
        )
        resource_dir, download_tmp_dir = tmp_path / "resources", tmp_path / "download"
        resource_dir.mkdir()
        download_tmp_dir.mkdir()

        def _run() -> Queue:
            report_que = Queue()
            download_resources_handler(
                helper.download_resources(
                    list(image.resources),
                    image.db_helper,
                    blob_storage_base_url=BASE_URL,
                    resource_dir=resource_dir,
                    download_tmp_dir=download_tmp_dir,
                ),
                metrics=mocker.MagicMock(),
                status_report_queue=report_que,
                session_id="session",
            )
            return report_que

        return SimpleNamespace(
            run=_run,
            helper=helper,
            resource_dir=resource_dir,
            download_tmp_dir=download_tmp_dir,
        )

    def test_every_blob_is_downloaded_once_and_every_resource_is_prepared(
        self, image, run, mocker: MockerFixture
    ):
        slice_tasks = mocker.spy(run.helper, "_download_slice_at_thread")

        report_que = run.run()

        # the 3 slices of the sliced resource and the 2 of the compressed blob are
        #   downloaded as tasks of their own, not by PrepareResourceHelper
        assert slice_tasks.call_count == 5
        assert image.storage.requests == Counter(dict.fromkeys(image.storage.blobs, 1))
        for _digest, _data in image.resources.items():
            assert (run.resource_dir / _digest.hex()).read_bytes() == _data
        assert list(run.download_tmp_dir.iterdir()) == []
        reports = [report_que.get_nowait().payload for _ in range(report_que.qsize())]
        assert sum(_r.processed_file_num for _r in reports) == len(image.resources)

    def test_a_slice_an_interrupted_ota_left_is_used_as_it_is(self, image, run):
        _slice_entry, _slice_data = image.a_slice_of_sliced
        _left = (
            run.download_tmp_dir
            / f"{_slice_entry.digest.hex()}_{image.sliced.resource_id}"
        )
        _left.write_bytes(_slice_data)

        run.run()

        assert image.storage.requests[_slice_entry.digest.hex()] == 0
        _sliced_digest = image.sliced.digest
        assert (
            run.resource_dir / _sliced_digest.hex()
        ).read_bytes() == image.resources[_sliced_digest]

    def test_a_slice_whose_download_failed_is_downloaded_by_the_resource(
        self, image, run
    ):
        _slice_entry, _ = image.a_slice_of_sliced
        _failed = _slice_entry.digest.hex()
        image.storage.fail_first.add(_failed)

        report_que = run.run()

        # once by its slice task (failed), once by PrepareResourceHelper
        assert image.storage.requests[_failed] == 2
        assert all(
            _count == 1
            for _name, _count in image.storage.requests.items()
            if _name != _failed
        )
        for _digest, _data in image.resources.items():
            assert (run.resource_dir / _digest.hex()).read_bytes() == _data
        reports = [report_que.get_nowait().payload for _ in range(report_que.qsize())]
        assert sum(_r.processed_file_num for _r in reports) == len(image.resources)
        assert sum(_r.errors for _r in reports) >= 1

    def test_a_compressed_blob_an_interrupted_ota_joined_is_used_as_it_is(
        self, image, run
    ):
        _blob_entry, _blob = image.compressed_blob
        (run.download_tmp_dir / _blob_entry.digest.hex()).write_bytes(_blob)

        run.run()

        for _slice in image.compressed_blob_slices:
            assert image.storage.requests[_slice.digest.hex()] == 0
        _digest = image.compressed.digest
        assert (run.resource_dir / _digest.hex()).read_bytes() == image.resources[
            _digest
        ]
