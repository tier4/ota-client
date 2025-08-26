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
"""Try to re-use the OTA resources from previous interrupted OTA when using inplace update mode."""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from queue import Queue

from otaclient._status_monitor import StatusReport, UpdateProgressReport
from otaclient.configs.cfg import cfg
from otaclient_common import EMPTY_FILE_SHA256
from otaclient_common._io import _gen_tmp_fname

from ._common import ResourcesDigestWithSize

logger = logging.getLogger(__name__)

REPORT_INTERVAL = 3  # second


class _ResourceOperatorBase:
    session_id: str
    _thread_local: threading.local
    _internal_que: Queue[int | None]
    _status_report_queue: Queue[StatusReport]

    def _remove_entry(self, digest_hex: str, dir_fd: int):
        with contextlib.suppress(Exception):
            os.unlink(digest_hex, dir_fd=dir_fd)

    def _thread_initializer(self):
        self._thread_local.buffer = _buffer = bytearray(cfg.READ_CHUNK_SIZE)
        self._thread_local.bufferview = memoryview(_buffer)

    def _report_uploader_thread(self) -> None:
        """Report uploader worker thread entry."""
        _merged_report = UpdateProgressReport(
            operation=UpdateProgressReport.Type.PREPARE_LOCAL_COPY
        )
        next_push = 0
        while (_entry := self._internal_que.get()) is not None:
            _merged_report.processed_file_num += 1
            _merged_report.processed_file_size += _entry

            _now = time.perf_counter()
            if _now > next_push:
                self._status_report_queue.put_nowait(
                    StatusReport(
                        payload=_merged_report,
                        session_id=self.session_id,
                    )
                )
                next_push = _now + REPORT_INTERVAL
                _merged_report = UpdateProgressReport(
                    operation=UpdateProgressReport.Type.PREPARE_LOCAL_COPY
                )

        # don't forget the last batch of reports
        if _merged_report.processed_file_num > 0:
            self._status_report_queue.put_nowait(
                StatusReport(
                    payload=_merged_report,
                    session_id=self.session_id,
                )
            )


class ResourceScanner(_ResourceOperatorBase):
    """Scan and verify OTA resource folder leftover by previous OTA.

    NOTE that this doesn't mean resuming the previous OTA, this helper only tries to
        re-use the OTA resources generated in previous OTA to speed up current OTA.
    """

    def __init__(
        self,
        *,
        all_resource_digests: ResourcesDigestWithSize,
        resource_dir: Path,
        status_report_queue: Queue[StatusReport],
        session_id: str,
    ) -> None:
        self._status_report_queue = status_report_queue
        self.session_id = session_id
        self._resource_dir = resource_dir
        self._all_resource_digests = all_resource_digests

        self._se = threading.Semaphore(cfg.MAX_CONCURRENT_PROCESS_FILE_TASKS)
        self._thread_local = threading.local()
        self._internal_que: Queue[int | None] = Queue()

    def _process_resource_at_thread(
        self, expected_digest: bytes, expected_digest_hex: str, *, dir_fd: int
    ) -> None:
        try:
            hash_f, file_size = sha256(), 0
            buffer, bufferview = (
                self._thread_local.buffer,
                self._thread_local.bufferview,
            )

            src_fd = os.open(expected_digest_hex, os.O_RDONLY, dir_fd=dir_fd)
            with open(src_fd, "rb") as src:
                os.posix_fadvise(src_fd, 0, 0, os.POSIX_FADV_NOREUSE)
                os.posix_fadvise(src_fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
                while read_size := src.readinto(buffer):
                    file_size += read_size
                    hash_f.update(bufferview[:read_size])
                os.posix_fadvise(src_fd, 0, 0, os.POSIX_FADV_DONTNEED)

            calculated_digest = hash_f.digest()
            if calculated_digest != expected_digest:
                self._remove_entry(expected_digest_hex, dir_fd)
                return

            try:
                self._all_resource_digests.pop(calculated_digest)
                self._internal_que.put_nowait(file_size)

            except KeyError:
                # basically should not happen, as now we only scan resources presented in
                #   the target OTA image now.
                pass
        finally:
            self._se.release()

    def resume_ota(self) -> None:
        """Scan the OTA resource folder leftover by previous interrupted OTA."""
        _resource_dir_fd = os.open(self._resource_dir, os.O_RDONLY)
        status_reporter_t = threading.Thread(
            target=self._report_uploader_thread,
            name="resume_ota_status_reporter",
            daemon=True,
        )
        status_reporter_t.start()
        try:
            with ThreadPoolExecutor(
                max_workers=cfg.MAX_PROCESS_FILE_THREAD,
                initializer=self._thread_initializer,
            ) as pool:
                _count = 0
                for entry in os.scandir(self._resource_dir):
                    _entry_digest_hex = entry.name
                    if _entry_digest_hex == EMPTY_FILE_SHA256:
                        continue

                    # NOTE: in resource dir, all files except tmp files are named
                    #       with its sha256 digest in hex string.
                    try:
                        expected_digest = bytes.fromhex(_entry_digest_hex)
                    except ValueError:
                        self._remove_entry(_entry_digest_hex, _resource_dir_fd)
                        continue

                    # NOTE(20250821): only scan resources that we need, for resources we don't need,
                    #                 just remove it from the resources dir.
                    if expected_digest not in self._all_resource_digests:
                        self._remove_entry(_entry_digest_hex, _resource_dir_fd)
                        continue

                    self._se.acquire()
                    _count += 1
                    pool.submit(
                        self._process_resource_at_thread,
                        expected_digest,
                        _entry_digest_hex,
                        dir_fd=_resource_dir_fd,
                    )

            logger.info(f"totally {_count} of OTA resource files are scanned")
        except Exception as e:
            logger.warning(f"exception during scanning OTA resource dir: {e!r}")
        finally:
            os.close(_resource_dir_fd)
            self._internal_que.put_nowait(None)
            status_reporter_t.join()


class ResourceStreamer(_ResourceOperatorBase):
    """Scan and verify OTA resource folder at the source slot, and stream with verifying it
    to the destination slot.
    """

    def __init__(
        self,
        *,
        all_resource_digests: ResourcesDigestWithSize,
        src_resource_dir: Path,
        dst_resource_dir: Path,
        status_report_queue: Queue[StatusReport],
        session_id: str,
    ) -> None:
        self._status_report_queue = status_report_queue
        self.session_id = session_id
        self._src_resource_dir = src_resource_dir
        self._dst_resource_dir = dst_resource_dir
        self._all_resource_digests = all_resource_digests

        self._se = threading.Semaphore(cfg.MAX_CONCURRENT_PROCESS_FILE_TASKS)
        self._thread_local = threading.local()
        self._internal_que: Queue[int | None] = Queue()

    def _process_resource_at_thread(
        self,
        expected_digest: bytes,
        expected_digest_hex: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        tmp_dst_fname = _gen_tmp_fname()
        try:
            hash_f, file_size = sha256(), 0
            buffer, bufferview = (
                self._thread_local.buffer,
                self._thread_local.bufferview,
            )

            src_fd = os.open(expected_digest_hex, os.O_RDONLY, dir_fd=src_dir_fd)
            tmp_dst_fd = os.open(
                tmp_dst_fname,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                dir_fd=dst_dir_fd,
            )

            with open(src_fd, "rb") as src, open(tmp_dst_fd, "wb") as tmp_dst:
                os.posix_fadvise(src_fd, 0, 0, os.POSIX_FADV_NOREUSE)
                os.posix_fadvise(src_fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
                os.posix_fadvise(tmp_dst_fd, 0, 0, os.POSIX_FADV_NOREUSE)
                os.posix_fadvise(tmp_dst_fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
                while read_size := src.readinto(buffer):
                    file_size += read_size
                    tmp_dst.write(bufferview[:read_size])
                    hash_f.update(bufferview[:read_size])
                os.posix_fadvise(src_fd, 0, 0, os.POSIX_FADV_DONTNEED)
                os.posix_fadvise(tmp_dst_fd, 0, 0, os.POSIX_FADV_DONTNEED)

            calculated_digest = hash_f.digest()
            if calculated_digest != expected_digest:
                self._remove_entry(tmp_dst_fname, dir_fd=dst_dir_fd)
                return

            try:
                self._all_resource_digests.pop(calculated_digest)
                # NOTE: both src and dst are under the same folder now
                os.replace(
                    tmp_dst_fname,
                    expected_digest_hex,
                    src_dir_fd=dst_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )
                self._internal_que.put_nowait(file_size)

            # basically should not happen, as now we only scan resources presented in
            #   the target OTA image now.
            except KeyError:
                pass
        finally:
            self._se.release()
            self._remove_entry(tmp_dst_fname, dir_fd=dst_dir_fd)

    def resume_ota(self) -> None:
        """Scan the OTA resource folder leftover by previous interrupted OTA."""
        src_dir_fd = os.open(self._src_resource_dir, os.O_RDONLY)
        dst_dir_fd = os.open(self._dst_resource_dir, os.O_RDONLY)

        status_reporter_t = threading.Thread(
            target=self._report_uploader_thread,
            name="resume_ota_status_reporter",
            daemon=True,
        )
        status_reporter_t.start()
        # NOTE: create a shallow copy of all digests
        _all_digests = set(self._all_resource_digests)
        try:
            with ThreadPoolExecutor(
                max_workers=cfg.MAX_PROCESS_FILE_THREAD,
                initializer=self._thread_initializer,
            ) as pool:
                _count = 0

                for entry in os.scandir(self._src_resource_dir):
                    _entry_digest_hex = entry.name
                    if _entry_digest_hex == EMPTY_FILE_SHA256:
                        continue

                    try:
                        _expected_digest = bytes.fromhex(_entry_digest_hex)
                    except ValueError:
                        continue

                    if _expected_digest not in _all_digests:
                        continue

                    self._se.acquire()
                    _count += 1
                    pool.submit(
                        self._process_resource_at_thread,
                        _expected_digest,
                        _entry_digest_hex,
                        src_dir_fd=src_dir_fd,
                        dst_dir_fd=dst_dir_fd,
                    )
            logger.info(f"totally {_count} of OTA resource files are scanned")
        except Exception as e:
            logger.warning(
                f"exception during scanning active slot's OTA resource dir: {e!r}"
            )
        finally:
            os.close(src_dir_fd)
            os.close(dst_dir_fd)
            self._internal_que.put_nowait(None)
            status_reporter_t.join()
