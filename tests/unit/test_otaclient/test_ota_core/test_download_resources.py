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
"""Tests for the OTA image metafiles downloading of the download helpers."""

from __future__ import annotations

import threading
from hashlib import sha256
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from otaclient.ota_core._download_resources import DownloadHelperForOTAImageV1
from otaclient_common.download_info import DownloadInfo
from otaclient_common.downloader import Downloader, DownloaderPool, DownloadResult


def _download_info(name: str) -> DownloadInfo:
    return DownloadInfo(
        url=f"https://example.com/ota/{name}",
        dst=Path(f"/tmp/{name}"),
        original_size=1,
        digest=name,
    )


class TestDownloadMetafilesBatch:
    @pytest.fixture
    def helper_with_pool(self, mocker: MockerFixture):
        pool = mocker.MagicMock(spec=DownloaderPool)
        pool.instance_num = 4
        helper = DownloadHelperForOTAImageV1(
            downloader_pool=pool, max_concurrent=4, download_inactive_timeout=10
        )
        # the worker thread of the metafiles download pool has its own downloader
        worker_downloader = mocker.MagicMock()
        helper._downloader_mapper[threading.get_native_id()] = worker_downloader
        return helper, pool, worker_downloader

    def test_batch_files_are_downloaded_concurrently(
        self, helper_with_pool, mocker: MockerFixture
    ):
        helper, pool, worker_downloader = helper_with_pool
        batch = [_download_info("file_table"), _download_info("resource_table")]

        # both downloads must be in flight at the same time to pass the barrier,
        #   downloading the batch one file after another breaks it on timeout.
        barrier = threading.Barrier(len(batch), timeout=10)

        def _download(*, url: str, **_) -> DownloadResult:
            barrier.wait()
            return DownloadResult(download_size=len(url))

        spare_downloader = mocker.MagicMock()
        spare_downloader.download.side_effect = _download
        worker_downloader.download.side_effect = _download
        pool.get_instance.return_value = spare_downloader

        res = helper._download_with_condition_at_thread(batch, threading.Condition())

        assert [_r.download_size for _r in res] == [len(_i.url) for _i in batch]
        # the worker downloads the first file, a spare instance the other one
        assert worker_downloader.download.call_args.kwargs["url"] == batch[0].url
        assert spare_downloader.download.call_args.kwargs["url"] == batch[1].url
        assert pool.get_instance.call_count == pool.release_instance.call_count == 1

    def test_batch_failure_is_raised_and_instances_released(
        self, helper_with_pool, mocker: MockerFixture
    ):
        helper, pool, worker_downloader = helper_with_pool
        batch = [_download_info(_name) for _name in ("regulars", "dirs", "symlinks")]

        worker_downloader.download.return_value = DownloadResult()
        failed, ok = mocker.MagicMock(), mocker.MagicMock()
        failed.download.side_effect = ValueError("broken stream")
        ok.download.return_value = DownloadResult()
        pool.get_instance.side_effect = [failed, ok]

        with pytest.raises(ValueError, match="broken stream"):
            helper._download_with_condition_at_thread(batch, threading.Condition())
        # every downloader instance taken from the pool is given back
        assert pool.get_instance.call_count == 2
        assert pool.release_instance.call_count == 2

    def test_single_file_uses_worker_downloader(self, helper_with_pool):
        helper, pool, worker_downloader = helper_with_pool
        worker_downloader.download.return_value = DownloadResult(download_size=1)

        res = helper._download_with_condition_at_thread(
            [_download_info("index.json")], threading.Condition()
        )

        assert len(res) == 1
        worker_downloader.download.assert_called_once()
        pool.get_instance.assert_not_called()

    def test_no_spare_downloader_falls_back_to_one_by_one(self, helper_with_pool):
        """All the instances of the pool are held by its worker threads."""
        helper, pool, worker_downloader = helper_with_pool
        batch = [_download_info("file_table"), _download_info("resource_table")]
        pool.get_instance.side_effect = ValueError("no idle downloader instance")
        worker_downloader.download.side_effect = lambda *, url, **_: DownloadResult(
            download_size=len(url)
        )

        res = helper._download_with_condition_at_thread(batch, threading.Condition())

        assert [_r.download_size for _r in res] == [len(_i.url) for _i in batch]
        assert worker_downloader.download.call_count == len(batch)
        pool.release_instance.assert_not_called()

    def test_files_without_a_spare_downloader_are_downloaded_by_the_worker(
        self, helper_with_pool, mocker: MockerFixture
    ):
        helper, pool, worker_downloader = helper_with_pool
        batch = [_download_info(_name) for _name in ("regulars", "dirs", "symlinks")]
        _lock = threading.Lock()

        def _get_instance():
            with _lock:  # one spare instance only
                if pool.get_instance.call_count > 1:
                    raise ValueError("no idle downloader instance")
            return spare_downloader

        spare_downloader = mocker.MagicMock()
        spare_downloader.download.side_effect = lambda *, url, **_: DownloadResult(
            download_size=len(url)
        )
        worker_downloader.download.side_effect = lambda *, url, **_: DownloadResult(
            download_size=len(url)
        )
        pool.get_instance.side_effect = _get_instance

        res = helper._download_with_condition_at_thread(batch, threading.Condition())

        # the results are in the order of the batch, whoever downloaded them
        assert [_r.download_size for _r in res] == [len(_i.url) for _i in batch]
        assert spare_downloader.download.call_count == 1
        assert worker_downloader.download.call_count == 2
        assert pool.release_instance.call_count == 1


class TestDownloadMetafilesWithDownloaderPool:
    """With a real DownloaderPool: instances are assigned per thread."""

    @pytest.fixture
    def pool(self):
        pool = DownloaderPool(instance_num=3, hash_func=sha256)
        try:
            yield pool
        finally:
            pool.shutdown()

    @pytest.fixture
    def downloads(self, mocker: MockerFixture):
        """Downloader.download patched: records the instance used for each file."""
        used: dict[str, int] = {}
        barriers: dict[str, threading.Barrier] = {}

        def _download(self: Downloader, *, url: str, **_) -> DownloadResult:
            used[url] = id(self)
            if _barrier := barriers.get(url):
                _barrier.wait()
            return DownloadResult(download_size=len(url))

        mocker.patch.object(
            Downloader, "download", autospec=True, side_effect=_download
        )
        return used, barriers

    def test_spare_instances_are_given_back_by_the_threads_that_took_them(
        self, pool: DownloaderPool, downloads
    ):
        helper = DownloadHelperForOTAImageV1(
            downloader_pool=pool, max_concurrent=4, download_inactive_timeout=10
        )
        helper._downloader_worker_initializer()  # this thread is the worker
        batch = [_download_info("file_table"), _download_info("resource_table")]

        helper._download_with_condition_at_thread(batch, threading.Condition())

        # only the worker's own instance is still taken
        assert list(pool._thread_idx_mapping) == [threading.get_native_id()]

    def test_metafiles_batches(self, pool: DownloaderPool, downloads):
        """Batches as OTA image v1 and legacy OTA image yield them, one at a time."""
        used, barriers = downloads
        batches = [
            [_download_info("index.json")],
            [_download_info("file_table"), _download_info("resource_table")],
            [_download_info(_name) for _name in ("regulars", "dirs", "symlinks")],
        ]
        for _batch in batches[1:]:
            # each file of a batch on an instance of its own, all at the same time
            _barrier = threading.Barrier(len(_batch), timeout=10)
            barriers.update((_info.url, _barrier) for _info in _batch)
        condition = threading.Condition()

        def _producer():
            for _batch in batches:
                with condition:
                    yield _batch
                    condition.wait()

        helper = DownloadHelperForOTAImageV1(
            downloader_pool=pool, max_concurrent=4, download_inactive_timeout=10
        )
        results = [
            _fut.result()
            for _fut in helper.download_meta_files(_producer(), condition=condition)
        ]

        assert [[_r.download_size for _r in _res] for _res in results] == [
            [len(_info.url) for _info in _batch] for _batch in batches
        ]
        for _batch in batches[1:]:
            assert len({used[_info.url] for _info in _batch}) == len(_batch)
        assert not pool._thread_idx_mapping
