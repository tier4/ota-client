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
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from otaclient.ota_core._download_resources import DownloadHelperForOTAImageV1
from otaclient_common.download_info import DownloadInfo
from otaclient_common.downloader import DownloaderPool, DownloadResult


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

        batch_downloaders = [mocker.MagicMock() for _ in batch]
        for _d in batch_downloaders:
            _d.download.side_effect = _download
        pool.get_instance.side_effect = batch_downloaders

        res = helper._download_with_condition_at_thread(batch, threading.Condition())

        assert [_r.download_size for _r in res] == [len(_i.url) for _i in batch]
        assert pool.get_instance.call_count == len(batch)
        assert pool.release_instance.call_count == len(batch)
        worker_downloader.download.assert_not_called()

    def test_batch_failure_is_raised_and_instances_released(
        self, helper_with_pool, mocker: MockerFixture
    ):
        helper, pool, _ = helper_with_pool
        batch = [_download_info("file_table"), _download_info("resource_table")]

        failed, ok = mocker.MagicMock(), mocker.MagicMock()
        failed.download.side_effect = ValueError("broken stream")
        ok.download.return_value = DownloadResult()
        pool.get_instance.side_effect = [failed, ok]

        with pytest.raises(ValueError, match="broken stream"):
            helper._download_with_condition_at_thread(batch, threading.Condition())
        # the failure may cancel the not yet started downloads of the batch, but
        #   every downloader instance taken from the pool is given back.
        assert pool.get_instance.call_count >= 1
        assert pool.release_instance.call_count == pool.get_instance.call_count

    def test_single_file_uses_worker_downloader(self, helper_with_pool):
        helper, pool, worker_downloader = helper_with_pool
        worker_downloader.download.return_value = DownloadResult(download_size=1)

        res = helper._download_with_condition_at_thread(
            [_download_info("index.json")], threading.Condition()
        )

        assert len(res) == 1
        worker_downloader.download.assert_called_once()
        pool.get_instance.assert_not_called()

    def test_no_spare_downloader_falls_back_to_one_by_one(
        self, helper_with_pool, mocker: MockerFixture
    ):
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
