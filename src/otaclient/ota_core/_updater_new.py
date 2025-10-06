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
from tempfile import TemporaryDirectory

from typing_extensions import Unpack

from ota_metadata.file_table.utils import save_fstable
from otaclient import errors as ota_errors
from otaclient._status_monitor import (
    OTAUpdatePhaseChangeReport,
    SetUpdateMetaReport,
    StatusReport,
)
from otaclient._types import CriticalZoneFlag, UpdatePhase
from otaclient._utils import wait_and_log
from otaclient.boot_control.protocol import BootControllerProtocol
from otaclient.configs.cfg import cfg, proxy_info
from otaclient.create_standby._common import ResourcesDigestWithSize
from otaclient.create_standby.update_slot import UpdateStandbySlot
from otaclient.create_standby.utils import can_use_in_place_mode
from otaclient.ota_core._download_resources import ResumeOTADownloadHelper
from otaclient_common import (
    _env,
    human_readable_size,
)
from otaclient_common._io import remove_file
from otaclient_common.linux import fstrim_at_subprocess

from ._update_libs import (
    DeltaCalCulator,
    download_resources_handler,
    process_persistents,
)
from ._updater_base import (
    OTAProtocol,
    OTAUpdateOperatorInitOTAImageV1,
    OTAUpdateOperatorOTAImageV1Base,
)

logger = logging.getLogger(__name__)

DEFAULT_STATUS_QUERY_INTERVAL = 1
WAIT_BEFORE_REBOOT = 6

STANDBY_SLOT_USED_SIZE_THRESHOLD = 0.8


class OTAUpdaterOTAImageV1(OTAUpdateOperatorOTAImageV1Base, OTAProtocol):
    """The implementation of OTA update logic."""

    def __init__(
        self,
        *,
        boot_controller: BootControllerProtocol,
        critical_zone_flag: CriticalZoneFlag,
        **kwargs: Unpack[OTAUpdateOperatorInitOTAImageV1],
    ):
        super().__init__(**kwargs)
        self.critical_zone_flag = critical_zone_flag
        self._boot_controller = boot_controller
        self._can_use_in_place_mode = False

    def _download_delta_resources(self, delta_digests: ResourcesDigestWithSize) -> None:
        """Download all the resources needed for the OTA update."""
        _download_tmp = self._download_tmp_on_standby
        if not _download_tmp.is_symlink() and _download_tmp.is_dir():
            logger.info(
                f"{_download_tmp} found, resuming previous interrupted OTA downloading ..."
            )
            try:
                _processed_entries = ResumeOTADownloadHelper(
                    _download_tmp,
                    self._ota_image_helper.resource_table_helper,
                    max_concurrent=cfg.MAX_CONCURRENT_DOWNLOAD_TASKS,
                )()
                logger.info(f"total {_processed_entries} are checked")
            except Exception as e:
                logger.warning(
                    "failed to recover the download dir from previous interrupted OTA, "
                    f"continue with cleanup the tmp download dir: {e}"
                )
                remove_file(_download_tmp)
        else:  # not a directory
            remove_file(_download_tmp)

        try:
            _download_tmp.mkdir(exist_ok=True)
            download_resources_handler(
                self._download_helper.download_resources(
                    delta_digests,
                    self._ota_image_helper.resource_table_helper,
                    blob_storage_base_url=self._ota_image_helper.blob_storage_url,
                    resource_dir=self._resource_dir_on_standby,
                    download_tmp_dir=self._download_tmp_on_standby,
                ),
                metrics=self._metrics,
                status_report_queue=self._status_report_queue,
                session_id=self.session_id,
            )
            # NOTE: only remove the download tmp when download finished successfully!
            #       this enables the OTA download resume feature.
            shutil.rmtree(_download_tmp, ignore_errors=True)
        except Exception as e:
            _err_msg = (
                "download aborted due to download stalls longer than "
                f"{cfg.DOWNLOAD_INACTIVE_TIMEOUT}, or otaclient process is terminated, abort OTA"
            )
            logger.error(_err_msg)
            raise ota_errors.NetworkError(_err_msg, module=__name__) from e
        finally:
            # NOTE: after this point, we don't need downloader anymore
            self._downloader_pool.shutdown()

    def _pre_update(self):
        """Pre-Update: Setting up boot control and preparing slots before OTA."""
        logger.info("enter local OTA update...")
        # NOTE(20250905): if ota_resources dir on active slot presented,
        #                 no need to use rebuild mode.
        _ota_resources_dir_presented = self._resource_dir_on_active.is_dir()
        image_config = self._ota_image_helper.image_config
        assert image_config

        with TemporaryDirectory() as _tmp_dir:
            self._can_use_in_place_mode = use_inplace_mode = can_use_in_place_mode(
                dev=self._boot_controller.standby_slot_dev,
                mnt_point=_tmp_dir,
                # NOTE: if sys_image_size is 0, always use inplace mode
                threshold_in_bytes=(
                    int(
                        (image_config.sys_image_size or 0)
                        * STANDBY_SLOT_USED_SIZE_THRESHOLD
                    )
                    if not _ota_resources_dir_presented
                    else None
                ),
            )
        logger.info(
            f"check if we can use in-place mode to update standby slot: {use_inplace_mode}"
        )
        self._metrics.use_inplace_mode = use_inplace_mode

        self._boot_controller.pre_update(
            # NOTE: this option is deprecated and not used by bootcontroller
            # NOTE(20250822): no matter we use inplace mode or not, always mount
            #                 mount the active slot also.
            standby_as_ref=use_inplace_mode,
            erase_standby=not use_inplace_mode,
        )

        # NOTE: for rebuild mode, discard will be done when formatting the standby slot
        if use_inplace_mode and cfg.FSTRIM_AT_OTA:
            _fstrim_timeout = cfg.FSTRIM_AT_OTA_TIMEOUT
            logger.info(
                f"on using inplace update mode, do fstrim on standby slot, {_fstrim_timeout=} ..."
            )
            fstrim_at_subprocess(
                self._boot_controller.get_standby_slot_path(),
                wait=True,
                timeout=cfg.FSTRIM_AT_OTA_TIMEOUT,
            )
            logger.info("fstrim done")

        # NOTE(20250529): first save it to /.ota-meta, and then save it to the actual
        #                 destination folder.
        logger.info("save the OTA image file_table to standby slot ...")
        self._ota_meta_store_on_standby.mkdir(exist_ok=True, parents=True)
        try:
            save_fstable(
                self._ota_image_helper._file_table_dbf, self._ota_meta_store_on_standby
            )
        except Exception as e:
            logger.error(
                f"failed to save OTA image file_table to {self._ota_meta_store_on_standby=}: {e!r}"
            )

    def _in_update(self):
        """In-Update: delta calculation, resources downloading and appply updates to standby slot."""
        logger.info("start to calculate delta ...")
        _delta_digests = DeltaCalCulator(
            file_table_db_helper=self._ota_image_helper.file_table_helper,
            standby_slot_mp=self._standby_slot_mp,
            active_slot_mp=self._active_slot_mp,
            status_report_queue=self._status_report_queue,
            session_id=self.session_id,
            metrics=self._metrics,
            use_inplace_mode=self._can_use_in_place_mode,
        ).calculate_delta()
        to_download = len(_delta_digests)
        to_download_size = sum(_delta_digests.values())
        logger.info(
            f"delta calculation finished: \n"
            f"download_list len: {to_download} \n"
            f"sum of original size of all resources to be downloaded: {human_readable_size(to_download_size)}"
        )

        _current_time = int(time.time())
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.DOWNLOADING_OTA_FILES,
                    trigger_timestamp=_current_time,
                ),
                session_id=self.session_id,
            )
        )
        self._metrics.download_start_timestamp = _current_time

        self._status_report_queue.put_nowait(
            StatusReport(
                payload=SetUpdateMetaReport(
                    total_download_files_num=to_download,
                    total_download_files_size=to_download_size,
                ),
                session_id=self.session_id,
            )
        )
        self._metrics.delta_download_files_num = to_download
        self._metrics.delta_download_files_size = to_download_size

        logger.info("start to download resources ...")
        self._download_delta_resources(_delta_digests)

        logger.info("start to apply changes to standby slot...")
        _current_time = int(time.time())
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.APPLYING_UPDATE,
                    trigger_timestamp=_current_time,
                ),
                session_id=self.session_id,
            )
        )
        self._metrics.apply_update_start_timestamp = _current_time

        try:
            standby_slot_creator = UpdateStandbySlot(
                file_table_db_helper=self._ota_image_helper.file_table_helper,
                standby_slot_mount_point=str(self._standby_slot_mp),
                status_report_queue=self._status_report_queue,
                session_id=self.session_id,
                resource_dir=self._resource_dir_on_standby,
            )
            standby_slot_creator.update_slot()
        except Exception as e:
            raise ota_errors.ApplyOTAUpdateFailed(
                f"failed to apply update to standby slot: {e!r}", module=__name__
            ) from e

    def _post_update(self) -> None:
        """Post-Update: configure boot control switch slot, persist files handling,
        preserve client squashfs image and OTA image metadata onto standby slot."""
        logger.info("enter post update phase...")
        _current_time = int(time.time())
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.PROCESSING_POSTUPDATE,
                    trigger_timestamp=_current_time,
                ),
                session_id=self.session_id,
            )
        )
        self._metrics.post_update_start_timestamp = _current_time

        # NOTE(20240219): move persist file handling at post_update hook
        if _persists := self._ota_image_helper.get_persistents_list():
            process_persistents(
                _persists,
                active_slot_mp=self._active_slot_mp,
                standby_slot_mp=self._standby_slot_mp,
            )

        self._preserve_ota_image_meta_at_post_update()
        # NOTE(20250823): secure the resource dir and metadata dir
        os.chmod(self._resource_dir_on_standby, 0o700)
        os.chmod(self._ota_meta_store_on_standby, 0o700)

        self._preserve_client_squashfs_at_post_update()
        self._boot_controller.post_update(self.update_version)

    def _finalize_update(self) -> None:
        """Finalize-Update: wait for all sub ECUs, and then reboot."""
        logger.info("local update finished, wait on all subecs...")
        _current_finalizing_time = int(time.time())
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.FINALIZING_UPDATE,
                    trigger_timestamp=_current_finalizing_time,
                ),
                session_id=self.session_id,
            )
        )
        self._metrics.finalizing_update_start_timestamp = _current_finalizing_time
        if proxy_info.enable_local_ota_proxy:
            wait_and_log(
                check_flag=self.ecu_status_flags.any_child_ecu_in_update.is_set,
                check_for=False,
                message="permit reboot flag",
                log_func=logger.info,
            )

        _current_reboot_time = int(time.time())
        self._metrics.reboot_start_timestamp = _current_reboot_time

        # publish the metrics before rebooting
        try:
            if self._shm_metrics_reader:
                _shm_metrics = self._shm_metrics_reader.sync_msg()
                self._metrics.shm_merge(_shm_metrics)
        except Exception as e:
            logger.error(f"failed to merge metrics: {e!r}")
        self._metrics.publish()

        logger.info(f"device will reboot in {WAIT_BEFORE_REBOOT} seconds!")
        time.sleep(WAIT_BEFORE_REBOOT)
        self._boot_controller.finalizing_update(
            chroot=_env.get_dynamic_client_chroot_path()
        )
