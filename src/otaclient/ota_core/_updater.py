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
"""Helpers classes and functions for implementing OTA update."""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from ota_image_libs.v1.file_table.db import FileTableDBHelper

from ota_metadata.file_table.utils import save_fstable
from ota_metadata.legacy2.metadata import ResourceMeta
from otaclient import errors as ota_errors
from otaclient._status_monitor import (
    OTAUpdatePhaseChangeReport,
    SetUpdateMetaReport,
    StatusReport,
)
from otaclient._types import UpdatePhase
from otaclient._utils import wait_and_log
from otaclient.boot_control.protocol import BootControllerProtocol
from otaclient.configs.cfg import cfg, ecu_info, proxy_info
from otaclient.create_standby._common import ResourcesDigestWithSize
from otaclient.create_standby.update_slot import UpdateStandbySlot
from otaclient.create_standby.utils import can_use_in_place_mode
from otaclient_common import (
    _env,
    human_readable_size,
)
from otaclient_common.cmdhelper import ensure_umount
from otaclient_common.linux import fstrim_at_subprocess

from ._update_libs import (
    DeltaCalCulator,
    download_resources_handler,
    process_persistents,
)
from ._updater_base import OTAUpdateOperatorLegacyOTAImage

logger = logging.getLogger(__name__)

DEFAULT_STATUS_QUERY_INTERVAL = 1
WAIT_BEFORE_REBOOT = 6

STANDBY_SLOT_USED_SIZE_THRESHOLD = 0.8


class OTAUpdater(OTAUpdateOperatorLegacyOTAImage):
    """The implementation of OTA update logic."""

    _boot_controller: BootControllerProtocol

    if not TYPE_CHECKING:

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if not self._boot_controller:
                raise ValueError

    def _download_delta_resources(self, delta_digests: ResourcesDigestWithSize) -> None:
        """Download all the resources needed for the OTA update."""
        _resource_meta = ResourceMeta(
            base_url=self.url_base,
            ota_metadata=self._ota_metadata,
            copy_dst=self._resource_dir_on_standby,
        )

        try:
            download_resources_handler(
                self._download_helper.download_resources(delta_digests, _resource_meta),
                metrics=self._metrics,
                status_report_queue=self._status_report_queue,
                session_id=self.session_id,
            )
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
            _resource_meta.shutdown()

    def _pre_update(self):
        """Prepare the standby slot and optimize the file_table."""
        logger.info("enter local OTA update...")
        # NOTE(20250905): if ota_resources dir on active slot presented,
        #                 no need to use rebuild mode.
        _ota_resources_dir_presented = self._resource_dir_on_active.is_dir()
        with TemporaryDirectory() as _tmp_dir:
            self._can_use_in_place_mode = use_inplace_mode = can_use_in_place_mode(
                dev=self._boot_controller.standby_slot_dev,
                mnt_point=_tmp_dir,
                threshold_in_bytes=(
                    int(
                        self._ota_metadata.total_regulars_size
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
            save_fstable(self._ota_metadata._fst_db, self._ota_meta_store_on_standby)
        except Exception as e:
            logger.error(
                f"failed to save OTA image file_table to {self._ota_meta_store_on_standby=}: {e!r}"
            )

    def _in_update(self):
        logger.info("start to calculate delta ...")
        _delta_digests = DeltaCalCulator(
            file_table_db_helper=FileTableDBHelper(self._ota_metadata.FSTABLE_DB),
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
                file_table_db_helper=FileTableDBHelper(self._ota_metadata._fst_db),
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

    def _preserve_ota_image_meta_at_post_update(self):
        self._ota_meta_store_on_standby.mkdir(exist_ok=True, parents=True)
        # after update_slot finished, we can finally remove the previous base file_table.
        shutil.rmtree(self._ota_meta_store_base_on_standby, ignore_errors=True)

        # save the filetable to /opt/ota/image-meta
        shutil.rmtree(self._image_meta_dir_on_standby, ignore_errors=True)
        self._image_meta_dir_on_standby.mkdir(exist_ok=True, parents=True)
        shutil.copytree(
            self._ota_meta_store_on_standby,
            self._image_meta_dir_on_standby,
            dirs_exist_ok=True,
        )

        # prepare base file_table to the base OTA meta store for next OTA
        self._ota_meta_store_base_on_standby.mkdir(exist_ok=True, parents=True)
        for entry in self._ota_meta_store_on_standby.iterdir():
            if entry.is_file():
                shutil.move(str(entry), self._ota_meta_store_base_on_standby)

    def _preserve_client_squashfs_at_post_update(self) -> None:
        """Copy the client squashfs file to the standby slot."""
        if not _env.is_dynamic_client_running():
            logger.info(
                "dynamic client is not running, no need to copy client squashfs file"
            )
            return

        _src = Path(cfg.ACTIVE_SLOT_MNT) / Path(
            cfg.DYNAMIC_CLIENT_SQUASHFS_FILE
        ).relative_to("/")
        _dst = Path(cfg.STANDBY_SLOT_MNT) / Path(
            cfg.OTACLIENT_INSTALLATION_RELEASE
        ).relative_to("/")
        logger.info(f"copy client squashfs file from {_src} to {_dst}...")
        try:
            os.makedirs(_dst, exist_ok=True)
            shutil.copy(_src, _dst, follow_symlinks=False)
        except FileNotFoundError as e:
            logger.warning(f"failed to copy client squashfs file: {e!r}")

    def _post_update(self) -> None:
        """Post-update phase."""
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
        process_persistents(
            self._ota_metadata.iter_persist_entries(),
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
        """Finalize the OTA update."""
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

    # API

    def execute(self) -> None:
        """Main entry for executing local OTA update.

        Handles OTA failure and logging/finalizing on failure.
        """
        logger.info(f"execute local update({ecu_info.ecu_id=}): {self.update_version=}")
        try:
            self._process_metadata()
            self._pre_update()
            self._in_update()
            self._post_update()
            self._finalize_update()
            # NOTE(20250818): not delete the OTA resource dir to speed up next OTA
        except ota_errors.OTAError as e:
            logger.error(f"update failed: {e!r}")
            self._boot_controller.on_operation_failure()
            raise  # do not cover the OTA error again
        except Exception as e:
            _err_msg = f"unspecific error, update failed: {e!r}"
            self._boot_controller.on_operation_failure()
            raise ota_errors.ApplyOTAUpdateFailed(_err_msg, module=__name__) from e
        finally:
            ensure_umount(self._session_workdir, ignore_error=True)
            shutil.rmtree(self._session_workdir, ignore_errors=True)
