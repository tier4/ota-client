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

from pathlib import Path

import pytest

from otaclient.ota_core._download_resources import (
    _BaseDownloadHelper,
    download_resumable,
)
from otaclient_common.download_info import DownloadInfo
from otaclient_common.downloader import DownloadResult, HashVerificationError

BASE_URL = "http://ota.example/data/"
DIGEST = "ab" * 32
SIZE = 1000


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
