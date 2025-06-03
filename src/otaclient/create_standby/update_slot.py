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
from pathlib import Path
from queue import Queue

from ota_metadata.file_table.utils import (
    RegularFileTypedDict,
    prepare_dir,
    prepare_non_regular,
    prepare_regular,
)
from ota_metadata.legacy2.metadata import OTAMetadata
from otaclient._status_monitor import StatusReport
from otaclient.configs.cfg import cfg
from otaclient.create_standby.delta_gen import (
    UpdateStandbySlotFailed,
)
from otaclient_common._logging import BurstSuppressFilter

logger = logging.getLogger(__name__)
burst_suppressed_logger = logging.getLogger(f"{__name__}.process_file_error")
# NOTE: for request_error, only allow max 6 lines of logging per 30 seconds
burst_suppressed_logger.addFilter(
    BurstSuppressFilter(
        f"{__name__}.process_file_error",
        upper_logger_name=__name__,
        burst_round_length=30,
        burst_max=6,
    )
)


class UpdateStandbySlot:
    def __init__(
        self,
        *,
        ota_metadata: OTAMetadata,
        standby_slot_mount_point: str,
        resource_dir: Path,
        status_report_queue: Queue[StatusReport],
        session_id: str,
        file_process_max_failure: int = cfg.CREATE_STANDBY_RETRY_MAX,
    ) -> None:
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

        try:
            for _entry in self._ota_metadata.iter_regular_entries():
                _this_digest = _entry["digest"]
                _inode_id = _entry["inode_id"]
                _is_hardlinked = _entry["links_count"] is not None

                # we always prepare the first copy by moving
                if _this_digest != cur_digest:
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
                    continue

                # not the first copy in this digest group
                if _is_hardlinked:
                    if _inode_id in hardlink_group:
                        prepare_regular(
                            _entry,
                            _rs=hardlink_group[_inode_id],
                            target_mnt=self._standby_slot_mp,
                            prepare_method="hardlink",
                        )
                    else:
                        hardlink_group[_inode_id] = prepare_regular(
                            _entry,
                            _rs=cur_resource,
                            target_mnt=self._standby_slot_mp,
                            prepare_method="copy",
                        )
                else:
                    prepare_regular(
                        _entry,
                        cur_resource,
                        target_mnt=self._standby_slot_mp,
                        prepare_method="copy",
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
                burst_suppressed_logger.exception(f"failed to process {entry=}: {e!r}")
                raise UpdateStandbySlotFailed(
                    f"failed to process {entry=}: {e!r}"
                ) from e

    def _process_non_regular_files(self) -> None:
        logger.info("start to process non-regular entries ...")
        for entry in self._ota_metadata.iter_non_regular_entries():
            try:
                prepare_non_regular(entry, target_mnt=self._standby_slot_mp)
            except Exception as e:
                burst_suppressed_logger.exception(f"failed to process {entry=}: {e!r}")
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
