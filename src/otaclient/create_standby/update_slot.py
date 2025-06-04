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
"""Generate delta from delta_src comparing to new OTA image."""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from queue import Queue

from ota_metadata.file_table.utils import (
    RegularFileTypedDict,
    prepare_dir,
    prepare_non_regular,
    prepare_regular,
)
from ota_metadata.legacy2.metadata import OTAMetadata
from otaclient._status_monitor import StatusReport, UpdateProgressReport
from otaclient.configs.cfg import cfg
from otaclient.create_standby.delta_gen import UpdateStandbySlotFailed

logger = logging.getLogger(__name__)


class UpdateStandbySlot:
    def __init__(
        self,
        *,
        ota_metadata: OTAMetadata,
        standby_slot_mount_point: str,
        resource_dir: Path,
        status_report_queue: Queue[StatusReport],
        session_id: str,
        status_report_interval: int = cfg.PROCESS_FILES_REPORT_INTERVAL,
    ) -> None:
        self.status_report_interval = status_report_interval
        self._ota_metadata = ota_metadata
        self._status_report_queue = status_report_queue
        self.session_id = session_id
        self._que: Queue[tuple[bytes, list[RegularFileTypedDict]] | None] = Queue()

        self._standby_slot_mp = Path(standby_slot_mount_point)
        self._resource_dir = Path(resource_dir)

    def _process_regular_file_entries(self) -> None:
        """Yield a group of regular file entries which have the same digest each time.

        NOTE: it depends on the regular file table is sorted by digest!
        """
        logger.info("start to process regular file entries ...")
        hardlink_group: dict[int, Path] = {}
        cur_digest: bytes = b""
        cur_resource: Path = Path()

        _next_report = 0
        _merged_payload = UpdateProgressReport(
            operation=UpdateProgressReport.Type.APPLY_DELTA
        )

        try:
            for _entry in self._ota_metadata.iter_regular_entries():
                _this_digest, _inode_id = _entry["digest"], _entry["inode_id"]
                _is_hardlinked = _entry["links_count"] is not None

                # NOTE that first copy will not be counted in report, as the first copy
                #   is either prepared by download, or by local, both are already recorded.
                if _this_digest != cur_digest:  # first copy
                    cur_digest = _this_digest
                    hardlink_group.clear()
                    cur_resource = prepare_regular(
                        _entry,
                        _rs=self._resource_dir / cur_digest.hex(),
                        target_mnt=self._standby_slot_mp,
                        prepare_method="move",
                    )

                    if _is_hardlinked:
                        hardlink_group[_inode_id] = cur_resource
                # not the first copy in this digest group
                elif _is_hardlinked:
                    _merged_payload.processed_file_num += 1
                    _merged_payload.processed_file_size += _entry["size"] or 0

                    # hardlinked entry shared the same inode, thus same permissions
                    if _inode_id in hardlink_group:
                        prepare_regular(
                            _entry,
                            _rs=hardlink_group[_inode_id],
                            target_mnt=self._standby_slot_mp,
                            prepare_method="hardlink",
                            hardlink_skip_apply_permission=True,
                        )
                    else:  # first entry in a hardlink group
                        hardlink_group[_inode_id] = prepare_regular(
                            _entry,
                            _rs=cur_resource,
                            target_mnt=self._standby_slot_mp,
                            prepare_method="copy",
                        )
                # normal multi copies
                else:
                    _merged_payload.processed_file_num += 1
                    _merged_payload.processed_file_size += _entry["size"] or 0

                    prepare_regular(
                        _entry,
                        cur_resource,
                        target_mnt=self._standby_slot_mp,
                        prepare_method="copy",
                    )

                if (_now := time.perf_counter()) > _next_report:
                    _next_report = _now + self.status_report_interval
                    self._status_report_queue.put_nowait(
                        StatusReport(
                            payload=_merged_payload,
                            session_id=self.session_id,
                        )
                    )
                    _merged_payload = UpdateProgressReport(
                        operation=UpdateProgressReport.Type.APPLY_DELTA
                    )

            # commit left-over items that cannot fill the batch
            self._status_report_queue.put_nowait(
                StatusReport(
                    payload=_merged_payload,
                    session_id=self.session_id,
                )
            )
        except Exception as e:
            logger.exception(f"failed to update standby slot: {e!r}")
            raise UpdateStandbySlotFailed(
                f"failed to update standby slot: {e!r}"
            ) from e

    def _process_dir_entries(self) -> None:
        logger.info("start to process directory entries ...")
        for entry in self._ota_metadata.iter_dir_entries():
            try:
                prepare_dir(entry, target_mnt=self._standby_slot_mp)
            except Exception as e:
                logger.exception(f"failed to process {entry=}: {e!r}")
                raise UpdateStandbySlotFailed(
                    f"failed to process {entry=}: {e!r}"
                ) from e

    def _process_non_regular_files(self) -> None:
        logger.info("start to process non-regular entries ...")
        for entry in self._ota_metadata.iter_non_regular_entries():
            try:
                prepare_non_regular(entry, target_mnt=self._standby_slot_mp)
            except Exception as e:
                logger.exception(f"failed to process {entry=}: {e!r}")
                raise UpdateStandbySlotFailed(
                    f"failed to process {entry=}: {e!r}"
                ) from e

    # API

    def update_slot(self) -> None:
        """
        Raises:
            UpdateStandbySlotFailed: if any error occurs during the process.
        """
        self._process_dir_entries()
        self._process_non_regular_files()
        self._process_regular_file_entries()

        # finally, cleanup the resource dir
        shutil.rmtree(self._resource_dir)
