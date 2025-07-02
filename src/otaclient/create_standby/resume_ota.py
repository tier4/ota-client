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
"""Try to resume previously interrupted OTA which uses inplace update mode."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from queue import Queue

from ota_metadata.legacy2.metadata import OTAMetadata
from ota_metadata.legacy2.rs_table import ResourceTableORMPool
from otaclient._status_monitor import StatusReport, UpdateProgressReport
from otaclient.configs.cfg import cfg


class ResourceScanner:
    """Scan and verify OTA resource folder to resume previous OTA."""

    def __init__(
        self,
        *,
        ota_metadata: OTAMetadata,
        resource_dir: Path,
        status_report_queue: Queue[StatusReport],
        session_id: str,
    ) -> None:
        self._status_report_queue = status_report_queue
        self.session_id = session_id
        self._resource_dir = resource_dir
        self._ota_metadata = ota_metadata

        self._rst_orm_pool = ResourceTableORMPool(
            con_factory=ota_metadata.connect_rstable, number_of_cons=3
        )
        self._se = threading.Semaphore(cfg.MAX_CONCURRENT_PROCESS_FILE_TASKS)
        self._thread_local = threading.local()

    def _thread_initializer(self):
        self._thread_local.buffer = _buffer = bytearray(cfg.READ_CHUNK_SIZE)
        self._thread_local.bufferview = memoryview(_buffer)

    def _process_resource_at_thread(self, fpath: Path, expected_digest: bytes) -> None:
        try:
            hash_f, file_size = sha256(), 0
            buffer, bufferview = (
                self._thread_local.buffer,
                self._thread_local.bufferview,
            )
            with open(fpath, "rb") as src:
                src_fd = src.fileno()
                os.posix_fadvise(src_fd, 0, 0, os.POSIX_FADV_NOREUSE)
                os.posix_fadvise(src_fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
                while read_size := src.readinto(buffer):
                    file_size += read_size
                    hash_f.update(bufferview[:read_size])
                os.posix_fadvise(src_fd, 0, 0, os.POSIX_FADV_DONTNEED)

            calculated_digest = hash_f.digest()
            if (
                calculated_digest != expected_digest
                or self._rst_orm_pool.orm_delete_entries(digest=calculated_digest) != 1
            ):
                fpath.unlink(missing_ok=True)
            else:
                self._status_report_queue.put_nowait(
                    StatusReport(
                        payload=UpdateProgressReport(
                            operation=UpdateProgressReport.Type.PREPARE_LOCAL_COPY,
                            processed_file_num=1,
                            processed_file_size=file_size,
                        ),
                        session_id=self.session_id,
                    )
                )
        finally:
            self._se.release()

    def resume_ota(self) -> None:
        with ThreadPoolExecutor(
            max_workers=cfg.MAX_PROCESS_FILE_THREAD,
            initializer=self._thread_initializer,
        ) as pool:
            for entry in os.scandir(self._resource_dir):
                _fname = entry.name
                # NOTE: in resource dir, all files except tmp files are named
                #       with its sha256 digest in hex string.
                try:
                    expected_digest = bytes.fromhex(_fname)
                except ValueError:
                    continue

                self._se.acquire()
                pool.submit(
                    self._process_resource_at_thread,
                    Path(entry.path),
                    expected_digest,
                )
