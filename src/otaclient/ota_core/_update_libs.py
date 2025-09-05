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

import logging
import os
import shutil
import time
from pathlib import Path
from queue import Queue
from typing import Iterable

from ota_image_libs.v1.file_table.db import FileTableDBHelper

from ota_metadata.file_table.utils import find_saved_fstable
from otaclient import errors as ota_errors
from otaclient._status_monitor import (
    OTAUpdatePhaseChangeReport,
    StatusReport,
)
from otaclient._types import UpdatePhase
from otaclient.configs.cfg import cfg
from otaclient.create_standby._common import ResourcesDigestWithSize
from otaclient.create_standby.delta_gen import (
    DeltaGenParams,
    InPlaceDeltaGenFullDiskScan,
    InPlaceDeltaWithBaseFileTable,
    RebuildDeltaGenFullDiskScan,
    RebuildDeltaWithBaseFileTable,
)
from otaclient.create_standby.resume_ota import ResourceScanner, ResourceStreamer
from otaclient.metrics import OTAMetricsData
from otaclient_common import (
    SHA256DIGEST_HEX_LEN,
    replace_root,
)
from otaclient_common._typing import StrOrPath
from otaclient_common.persist_file_handling import PersistFilesHandler

logger = logging.getLogger(__name__)


class DeltaCalCulator:
    def __init__(
        self,
        *,
        file_table_db_helper: FileTableDBHelper,
        standby_slot_mp: Path,
        active_slot_mp: Path,
        status_report_queue: Queue[StatusReport],
        session_id: str,
        metrics: OTAMetricsData,
        use_inplace_mode: bool,
    ) -> None:
        self._status_report_queue = status_report_queue
        self._fst_db_helper = file_table_db_helper
        self.session_id = session_id
        self._metrics = metrics
        self._use_inplace_mode = use_inplace_mode

        # standby slot
        self._standby_slot_mp = standby_slot_mp
        self._resource_dir_on_standby = Path(
            replace_root(
                cfg.OTA_RESOURCES_STORE,
                cfg.CANONICAL_ROOT,
                standby_slot_mp,
            )
        )
        self._image_meta_dir_on_standby = Path(
            replace_root(
                cfg.IMAGE_META_DPATH,
                cfg.CANONICAL_ROOT,
                self._standby_slot_mp,
            )
        )
        self._ota_meta_store_on_standby = Path(
            replace_root(
                cfg.OTA_META_STORE,
                cfg.CANONICAL_ROOT,
                standby_slot_mp,
            )
        )
        self._ota_meta_store_base_on_standby = Path(
            replace_root(
                cfg.OTA_META_STORE_BASE_FILE_TABLE,
                cfg.CANONICAL_ROOT,
                standby_slot_mp,
            )
        )

        # active slot
        self._active_slot_mp = active_slot_mp
        self._resource_dir_on_active = Path(
            replace_root(
                cfg.OTA_RESOURCES_STORE,
                cfg.CANONICAL_ROOT,
                active_slot_mp,
            )
        )
        self._image_meta_dir_on_active = Path(
            replace_root(
                cfg.IMAGE_META_DPATH,
                cfg.CANONICAL_ROOT,
                active_slot_mp,
            )
        )

    def _find_base_filetable_for_inplace_mode_at_delta_cal(self) -> StrOrPath | None:
        """
        Returns:
            Verfied base file_table fpath, or None if failed to find one.
        """
        # NOTE: if the previous OTA is interrupted, and it is base file_table assisted,
        #       try to keep using that base file_table.
        verified_base_db = None
        if self._ota_meta_store_base_on_standby.is_dir():
            verified_base_db = find_saved_fstable(self._ota_meta_store_base_on_standby)

        if verified_base_db is None:
            shutil.rmtree(self._ota_meta_store_base_on_standby, ignore_errors=True)
            # NOTE: the file_table file in /opt/ota/image-meta MUST be prepared by otaclient,
            #       it is not included in the OTA image, thus also not in file_table.
            if self._image_meta_dir_on_standby.is_dir():
                shutil.move(
                    str(self._image_meta_dir_on_standby),
                    self._ota_meta_store_base_on_standby,
                )
                verified_base_db = find_saved_fstable(
                    self._ota_meta_store_base_on_standby
                )
        return verified_base_db

    def _copy_from_active_slot_at_delta_cal(
        self, delta_digests: ResourcesDigestWithSize
    ) -> None:
        """Copy resources from active slot's OTA resources dir."""
        if self._resource_dir_on_active.is_dir():
            logger.info(
                "active slot's OTA resource dir available, try to collect resources from it ..."
            )
            ResourceStreamer(
                all_resource_digests=delta_digests,
                src_resource_dir=self._resource_dir_on_active,
                dst_resource_dir=self._resource_dir_on_standby,
                status_report_queue=self._status_report_queue,
                session_id=self.session_id,
            ).resume_ota()
            logger.info("finish up copying from active_slot OTA resource dir")

    def _backward_compat_for_ota_tmp_at_delta_cal(self):
        """Backward compatibility for .ota-tmp, try to migrate from .ota-tmp if presented.

        NOTE(20250825): in case of OTA by previous otaclient interrupted, migrate the
                        old /.ota-tmp to new /.ota-resources.
        NOTE(20250825): the case of "OTA interrupted with older otaclient, and then retried with new otaclient
                          and interrupted again, and then retried with older otaclient again" is NOT SUPPORTED!
                        User should finish up the OTA with new otaclient in the above case.
        """
        _ota_tmp_dir_on_standby = Path(
            replace_root(
                cfg.OTA_TMP_STORE,
                cfg.CANONICAL_ROOT,
                self._standby_slot_mp,
            )
        )
        if _ota_tmp_dir_on_standby.is_dir():
            logger.warning(
                f"detect .ota-tmp on standby slot {_ota_tmp_dir_on_standby}, "
                "potential interrupted OTA by older otaclient, "
                f"try to migrate the resources to {self._resource_dir_on_standby}"
            )
            if self._resource_dir_on_standby.is_dir():
                for _entry in os.scandir(_ota_tmp_dir_on_standby):
                    _entry_name = _entry.name
                    if len(_entry.name) == SHA256DIGEST_HEX_LEN:
                        try:
                            bytes.fromhex(_entry_name)
                        except ValueError:
                            continue  # not an OTA resource file
                        os.replace(
                            _entry.path, self._resource_dir_on_standby / _entry_name
                        )
                shutil.rmtree(_ota_tmp_dir_on_standby, ignore_errors=True)
            else:
                os.replace(_ota_tmp_dir_on_standby, self._resource_dir_on_standby)

    def _resume_ota_for_inplace_mode_at_delta_cal(
        self, all_resource_digests: ResourcesDigestWithSize
    ):
        """For inplace update mode resume previous OTA progress.

        This method MUST be called before delta calculation, and ONLY for inplace mode.
        """
        if self._resource_dir_on_standby.is_dir():
            logger.info(
                "OTA resource dir found on standby slot, speed up delta calculation with it ..."
            )
            ResourceScanner(
                all_resource_digests=all_resource_digests,
                resource_dir=self._resource_dir_on_standby,
                status_report_queue=self._status_report_queue,
                session_id=self.session_id,
            ).resume_ota()
            logger.info("finish up scanning OTA resource dir")

    def calculate_delta(self) -> ResourcesDigestWithSize:
        """Calculate the delta bundle."""
        _current_time = int(time.time())
        all_resource_digests = ResourcesDigestWithSize.from_iterable(
            self._fst_db_helper.select_all_digests_with_size(),
        )

        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.CALCULATING_DELTA,
                    trigger_timestamp=_current_time,
                ),
                session_id=self.session_id,
            )
        )
        self._metrics.delta_calculation_start_timestamp = _current_time

        try:
            self._backward_compat_for_ota_tmp_at_delta_cal()
            if self._use_inplace_mode:
                self._resume_ota_for_inplace_mode_at_delta_cal(all_resource_digests)
                self._resource_dir_on_standby.mkdir(exist_ok=True, parents=True)

                _inplace_mode_params = DeltaGenParams(
                    file_table_db_helper=self._fst_db_helper,
                    all_resource_digests=all_resource_digests,
                    delta_src=self._standby_slot_mp,
                    copy_dst=self._resource_dir_on_standby,
                    status_report_queue=self._status_report_queue,
                    session_id=self.session_id,
                )

                verified_base_db = (
                    self._find_base_filetable_for_inplace_mode_at_delta_cal()
                )
                if verified_base_db:
                    logger.info("use in-place mode with base file table assist ...")
                    InPlaceDeltaWithBaseFileTable(**_inplace_mode_params).process_slot(
                        str(verified_base_db)
                    )
                else:
                    logger.info("use in-place mode with full scanning ...")
                    InPlaceDeltaGenFullDiskScan(**_inplace_mode_params).process_slot()

                # after inplace mode delta generation finished, try to collect any resources
                #   needed also from active slot.
                # NOTE(20250822): when we find that the delta size(uncompressed) is very large,
                #                   we might expect a major OS version bump.
                #                 In such case, when we do second OTA, with inplace update mode, even previously
                #                   we have already updated to the major OS version bump, 2nd OTA will still
                #                   need to download the delta again, as standby slot still holds old OS.
                #                 To cover this case, if delta size is too large, we will try to copy from active slot,
                #                   to avoid downloading files we have already downloaded previously.
                self._copy_from_active_slot_at_delta_cal(all_resource_digests)

            else:  # rebuild mode
                self._resource_dir_on_standby.mkdir(exist_ok=True, parents=True)
                # for rebuild mode, copy from active slot's resource dir first if possible
                self._copy_from_active_slot_at_delta_cal(all_resource_digests)

                _rebuild_mode_params = DeltaGenParams(
                    file_table_db_helper=self._fst_db_helper,
                    all_resource_digests=all_resource_digests,
                    delta_src=self._active_slot_mp,
                    copy_dst=self._resource_dir_on_standby,
                    status_report_queue=self._status_report_queue,
                    session_id=self.session_id,
                )

                verified_base_db = find_saved_fstable(self._image_meta_dir_on_active)
                if verified_base_db:
                    logger.info("use rebuild mode with base file table assist ...")
                    RebuildDeltaWithBaseFileTable(**_rebuild_mode_params).process_slot(
                        str(verified_base_db)
                    )
                else:
                    logger.info("use rebuild mode with full scanning ...")
                    RebuildDeltaGenFullDiskScan(**_rebuild_mode_params).process_slot()

            return all_resource_digests
        except Exception as e:
            _err_msg = f"failed to generate delta: {e!r}"
            logger.exception(_err_msg)
            raise ota_errors.UpdateDeltaGenerationFailed(
                _err_msg, module=__name__
            ) from e


def process_persistents(
    _entries: Iterable[str], *, active_slot_mp: Path, standby_slot_mp: Path
) -> None:
    """Implementation of preserving files accross slots at OTA post update."""
    logger.info("start persist files handling...")
    _handler = PersistFilesHandler(
        src_passwd_file=active_slot_mp / "etc/passwd",
        src_group_file=active_slot_mp / "etc/group",
        dst_passwd_file=standby_slot_mp / "etc/passwd",
        dst_group_file=standby_slot_mp / "etc/group",
        src_root=active_slot_mp,
        dst_root=standby_slot_mp,
    )

    for persiste_entry in _entries:
        # NOTE(20240520): with update_swapfile ansible role being used wildly,
        #   now we just ignore the swapfile entries in the persistents.txt if any,
        #   and issue a warning about it.
        if persiste_entry in ["/swapfile", "/swap.img"]:
            logger.warning(
                f"swapfile entry {persiste_entry} is listed in persistents.txt, ignored"
            )
            logger.warning(
                (
                    "using persis file feature to preserve swapfile is MISUSE of persist file handling feature!"
                    "please change your OTA image build setting and remove swapfile entries from persistents.txt!"
                )
            )
            continue

        try:
            _handler.preserve_persist_entry(persiste_entry)
        except Exception as e:
            _err_msg = f"failed to preserve {persiste_entry}: {e!r}, skip"
            logger.warning(_err_msg)
