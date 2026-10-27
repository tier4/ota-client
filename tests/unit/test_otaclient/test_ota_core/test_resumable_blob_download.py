# Copyright 2026 TIER IV, INC. All rights reserved.
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
"""A blob download continued where a retry finds it."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from ota_image_libs.v1.resource_table.utils import ResourceDownloadInfo

from otaclient.ota_core._download_resources import (
    PARTIAL_SUFFIX,
    RESUMABLE_BLOB_MIN_SIZE,
    DownloadHelperForOTAImageV1,
    _BaseDownloadHelper,
    _PartialDownloads,
    download_resumable,
)
from otaclient_common.download_info import DownloadInfo
from otaclient_common.downloader import DownloadResult, HashVerificationError

BASE_URL = "http://ota.example/data/"
DIGEST = "ab" * 32
SIZE = 1000
LARGE = RESUMABLE_BLOB_MIN_SIZE
SMALL = RESUMABLE_BLOB_MIN_SIZE - 1


class FakeDownloader:
    """Writes the rest of the blob from <resume_from>, or fails as told."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._fail_with = fail_with

    def download(self, url, dst, *, resume_from=0, size=None, **kwargs):
        self.calls.append(
            dict(url=url, dst=Path(dst), resume_from=resume_from, size=size, **kwargs)
        )
        if self._fail_with is not None:
            raise self._fail_with
        _size = size or 1
        with open(dst, "r+b" if resume_from else "wb") as f:
            f.seek(resume_from)
            f.write(b"x" * (_size - resume_from))
        return DownloadResult(0, _size - resume_from, _size - resume_from)


class TestDownloadResumable:
    def test_what_an_interrupted_download_left_is_continued(self, tmp_path: Path):
        _part = tmp_path / f"{DIGEST}.part"
        _part.write_bytes(b"x" * (SIZE // 2))
        _downloader = FakeDownloader()

        download_resumable(
            _downloader,  # type: ignore[arg-type]
            url=BASE_URL + DIGEST,
            part=_part,
            digest=DIGEST,
            size=SIZE,
        )

        (_call,) = _downloader.calls
        assert _call["dst"] == _part and _call["resume_from"] == SIZE // 2
        assert _call["size"] == SIZE and _call["digest"] == DIGEST
        assert _part.stat().st_size == SIZE

    @pytest.mark.parametrize("have", [SIZE, SIZE + 1], ids=["whole", "longer"])
    def test_a_part_not_shorter_than_the_blob_is_not_continued(
        self, tmp_path: Path, have: int
    ):
        """It is not a prefix of the blob, whatever it is."""
        _part = tmp_path / f"{DIGEST}.part"
        _part.write_bytes(b"x" * have)
        _downloader = FakeDownloader()

        download_resumable(
            _downloader,  # type: ignore[arg-type]
            url=BASE_URL,
            part=_part,
            digest=DIGEST,
            size=SIZE,
        )

        assert _downloader.calls[0]["resume_from"] == 0
        assert _part.stat().st_size == SIZE

    def test_an_interrupted_download_leaves_its_bytes_for_the_retry(
        self, tmp_path: Path
    ):
        _part = tmp_path / f"{DIGEST}.part"
        _part.write_bytes(b"x" * 10)

        with pytest.raises(ConnectionError):
            download_resumable(
                FakeDownloader(fail_with=ConnectionError("link dropped")),  # type: ignore[arg-type]
                url=BASE_URL,
                part=_part,
                digest=DIGEST,
                size=SIZE,
            )

        assert _part.stat().st_size == 10

    def test_bytes_that_do_not_hash_to_the_digest_are_dropped(self, tmp_path: Path):
        """They are not this blob, not even in part: the retry must not build on them."""
        _part = tmp_path / f"{DIGEST}.part"
        _part.write_bytes(b"x" * 10)

        with pytest.raises(HashVerificationError):
            download_resumable(
                FakeDownloader(fail_with=HashVerificationError("nope")),  # type: ignore[arg-type]
                url=BASE_URL,
                part=_part,
                digest=DIGEST,
                size=SIZE,
            )

        assert not _part.exists()


class TestDownloadOne:
    """`DownloadInfo.resume`: what the download helper does with it."""

    def test_a_resumable_download_is_continued(self, tmp_path: Path):
        _part = tmp_path / f"{DIGEST}.part"
        _part.write_bytes(b"x" * 10)
        _downloader = FakeDownloader()

        _BaseDownloadHelper._download_one(
            _downloader,  # type: ignore[arg-type]
            DownloadInfo(
                url=BASE_URL + DIGEST,
                dst=_part,
                original_size=SIZE,
                digest=DIGEST,
                resume=True,
            ),
        )

        (_call,) = _downloader.calls
        assert _call["resume_from"] == 10 and _call["dst"] == _part
        assert _call["digest"] == DIGEST and _call["size"] == SIZE

    def test_a_download_is_fetched_whole_as_before(self, tmp_path: Path):
        _dst = tmp_path / "file"
        _dst.write_bytes(b"x" * 10)
        _downloader = FakeDownloader()

        _BaseDownloadHelper._download_one(
            _downloader,  # type: ignore[arg-type]
            DownloadInfo(url=BASE_URL + DIGEST, dst=_dst, original_size=SIZE),
        )

        (_call,) = _downloader.calls
        assert _call["resume_from"] == 0 and _call["dst"] == _dst


#
# ------ OTA image v1: which blobs go through a .part ------ #
#


class TestPartialDownloads:
    def test_a_large_blob_goes_through_a_part_named_by_its_digest(self, tmp_path: Path):
        _digest = b"\x0a" * 32
        with _PartialDownloads(tmp_path).use(_digest, LARGE) as _part:
            assert _part == tmp_path / f"{_digest.hex()}{PARTIAL_SUFFIX}"

    def test_a_small_blob_is_downloaded_whole(self, tmp_path: Path):
        with _PartialDownloads(tmp_path).use(b"\x0a" * 32, SMALL) as _part:
            assert _part is None

    def test_a_part_is_written_by_one_thread_at_a_time(self, tmp_path: Path):
        """A slice two sliced resources share can be needed by both at once."""
        _partials, _digest = _PartialDownloads(tmp_path), b"\x0a" * 32
        with _partials.use(_digest, LARGE) as _first:
            with _partials.use(_digest, LARGE) as _second:
                assert _first is not None and _second is None
        with _partials.use(_digest, LARGE) as _again:
            assert _again == _first, "it is free again once the first is done"


def info(tmp_path: Path, size: int, *, digest: bytes = b"\x0a" * 32, **kwargs):
    return ResourceDownloadInfo(
        digest=digest, size=size, save_dst=tmp_path / "save_dst", **kwargs
    )


class TestWhichBlobsAreResumed:
    @pytest.fixture
    def helper_and_downloader(self):
        _helper = DownloadHelperForOTAImageV1(
            downloader_pool=MagicMock(instance_num=1),
            max_concurrent=1,
            download_inactive_timeout=60,
        )
        _downloader = FakeDownloader()
        _helper._downloader_mapper[threading.get_native_id()] = _downloader  # type: ignore[assignment]
        return _helper, _downloader

    def _download(self, helper, tmp_path: Path, _info: ResourceDownloadInfo):
        _rst_helper = MagicMock()
        _rst_helper.prepare_resource_at_thread.return_value = (
            MagicMock(size=_info.size),
            iter([_info]),
        )
        return helper._download_single_resource_at_thread(
            b"\x0b" * 32,
            _rst_helper=_rst_helper,
            _base_url=BASE_URL,
            _partials=_PartialDownloads(tmp_path),
        )

    def test_a_large_uncompressed_blob_is_resumable(
        self, tmp_path: Path, helper_and_downloader
    ):
        _helper, _downloader = helper_and_downloader
        _info = info(tmp_path, LARGE)
        _part = tmp_path / f"{_info.digest.hex()}{PARTIAL_SUFFIX}"
        _part.write_bytes(b"x" * 10)

        self._download(_helper, tmp_path, _info)

        (_call,) = _downloader.calls
        assert _call["dst"] == _part and _call["resume_from"] == 10
        assert _info.save_dst.stat().st_size == LARGE and not _part.exists()

    def test_a_small_blob_is_downloaded_to_where_it_goes(
        self, tmp_path: Path, helper_and_downloader
    ):
        _helper, _downloader = helper_and_downloader
        _info = info(tmp_path, SMALL)

        self._download(_helper, tmp_path, _info)

        (_call,) = _downloader.calls
        assert _call["dst"] == _info.save_dst and _call["resume_from"] == 0

    def test_a_compressed_blob_is_decompressed_as_it_arrives(
        self, tmp_path: Path, helper_and_downloader
    ):
        """There is no offset in a decompressed stream to continue from, and its cache
        key at an otaproxy stays the one it has always been."""
        _helper, _downloader = helper_and_downloader
        _info = info(
            tmp_path,
            LARGE,
            compression_alg="zstd",
            compressed_origin_digest=b"\x0c" * 32,
            compressed_origin_size=LARGE * 2,
        )

        self._download(_helper, tmp_path, _info)

        (_call,) = _downloader.calls
        assert _call["dst"] == _info.save_dst and _call["resume_from"] == 0
        assert _call["compression_alg"] == "zstd"
        assert _call["digest"] == (b"\x0c" * 32).hex()
