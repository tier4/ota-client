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
from tempfile import TemporaryDirectory

from ota_metadata.file_table.db import FileTableDBHelper
from ota_metadata.file_table.utils import find_saved_fstable, save_fstable
from ota_metadata.legacy2.metadata import OTAMetadata, ResourceMeta
from otaclient import errors as ota_errors
from otaclient._status_monitor import (
    OTAUpdatePhaseChangeReport,
    SetUpdateMetaReport,
    StatusReport,
    UpdateProgressReport,
)
from otaclient._types import UpdatePhase
from otaclient._utils import wait_and_log
from otaclient.boot_control import BootControllerProtocol
from otaclient.configs.cfg import cfg, ecu_info, proxy_info
from otaclient.create_standby._common import ResourcesDigestWithSize
from otaclient.create_standby.delta_gen import (
    DeltaGenParams,
    InPlaceDeltaGenFullDiskScan,
    InPlaceDeltaWithBaseFileTable,
    RebuildDeltaGenFullDiskScan,
    RebuildDeltaWithBaseFileTable,
)
from otaclient.create_standby.resume_ota import ResourceScanner, ResourceStreamer
from otaclient.create_standby.update_slot import UpdateStandbySlot
from otaclient.create_standby.utils import can_use_in_place_mode
from otaclient_common import (
    SHA256DIGEST_HEX_LEN,
    _env,
    human_readable_size,
    replace_root,
)
from otaclient_common._typing import StrOrPath
from otaclient_common.cmdhelper import ensure_umount
from otaclient_common.linux import fstrim_at_subprocess
from otaclient_common.persist_file_handling import PersistFilesHandler

from ._common import download_exception_handler
from ._updater_base import OTAUpdateOperator

logger = logging.getLogger(__name__)

DEFAULT_STATUS_QUERY_INTERVAL = 1
WAIT_BEFORE_REBOOT = 6
DOWNLOAD_STATS_REPORT_BATCH = 300
DOWNLOAD_REPORT_INTERVAL = 1  # second

STANDBY_SLOT_USED_SIZE_THRESHOLD = 0.8


class OTAUpdater(OTAUpdateOperator):
    """The implementation of OTA update logic."""

    def __init__(
        self,
        boot_controller: BootControllerProtocol,
        **kwargs,
    ) -> None:
        # ------ init base class ------ #
        super().__init__(**kwargs)

        # ------ init updater implementation ------ #
        self._boot_controller = boot_controller

        # ------ define runtime dirs ------ #
        self._active_slot_mp = Path(cfg.ACTIVE_SLOT_MNT)
        self._standby_slot_mp = self._boot_controller.get_standby_slot_path()
        self._resource_dir_on_standby = Path(
            replace_root(
                cfg.OTA_RESOURCES_STORE,
                cfg.CANONICAL_ROOT,
                self._standby_slot_mp,
            )
        )
        self._resource_dir_on_active = Path(
            replace_root(
                cfg.OTA_RESOURCES_STORE,
                cfg.CANONICAL_ROOT,
                self._active_slot_mp,
            )
        )

        self._ota_meta_store_on_standby = Path(
            replace_root(
                cfg.OTA_META_STORE,
                cfg.CANONICAL_ROOT,
                self._standby_slot_mp,
            )
        )
        self._ota_meta_store_base_on_standby = Path(
            replace_root(
                cfg.OTA_META_STORE_BASE_FILE_TABLE,
                cfg.CANONICAL_ROOT,
                self._standby_slot_mp,
            )
        )

        self._image_meta_dir_on_active = Path(
            replace_root(
                cfg.IMAGE_META_DPATH,
                cfg.CANONICAL_ROOT,
                self._active_slot_mp,
            )
        )
        self._image_meta_dir_on_standby = Path(
            replace_root(
                cfg.IMAGE_META_DPATH,
                cfg.CANONICAL_ROOT,
                self._standby_slot_mp,
            )
        )
        self._can_use_in_place_mode = False

    def _download_resources(self, delta_digests: ResourcesDigestWithSize) -> None:
        resource_meta = ResourceMeta(
            base_url=self.url_base,
            ota_metadata=self._ota_metadata,
            copy_dst=self._resource_dir_on_standby,
        )
        try:
            _next_commit_before, _report_batch_cnt = 0, 0
            _merged_payload = UpdateProgressReport(
                operation=UpdateProgressReport.Type.DOWNLOAD_REMOTE_COPY
            )
            for _done_count, _fut in enumerate(
                self._download_helper.download_resources(
                    delta_digests,
                    resource_meta,
                ),
                start=1,
            ):
                _now = time.time()
                if download_exception_handler(_fut):
                    err_count, file_size, downloaded_bytes = _fut.result()

                    _merged_payload.processed_file_num += 1
                    _merged_payload.processed_file_size += file_size
                    _merged_payload.errors += err_count
                    _merged_payload.downloaded_bytes += downloaded_bytes
                else:
                    _merged_payload.errors += 1

                self._metrics.downloaded_bytes = _merged_payload.downloaded_bytes
                self._metrics.downloaded_errors = _merged_payload.errors

                if (
                    _this_batch := _done_count // DOWNLOAD_STATS_REPORT_BATCH
                ) > _report_batch_cnt or _now > _next_commit_before:
                    _next_commit_before = _now + DOWNLOAD_REPORT_INTERVAL
                    _report_batch_cnt = _this_batch

                    self._status_report_queue.put_nowait(
                        StatusReport(
                            payload=_merged_payload,
                            session_id=self.session_id,
                        )
                    )

                    _merged_payload = UpdateProgressReport(
                        operation=UpdateProgressReport.Type.DOWNLOAD_REMOTE_COPY
                    )

            # for left-over items that cannot fill up the batch
            self._status_report_queue.put_nowait(
                StatusReport(
                    payload=_merged_payload,
                    session_id=self.session_id,
                )
            )
        finally:
            # up to this time, we don't need downloader anymore
            self._downloader_pool.shutdown()
            resource_meta.shutdown()

    def _execute_update(self):
        """Implementation of OTA updating."""
        logger.info(f"execute local update({ecu_info.ecu_id=}): {self.update_version=}")

        self._handle_upper_proxy()
        self._process_metadata()
        with self.critical_zone_flags:
            self._pre_update()
        _delta_digests = self._calculate_delta()
        self._download_delta_resources(_delta_digests)
        self._apply_update()
        with self.critical_zone_flags:
            self._post_update()
        with self.critical_zone_flags:
            self._finalize_update()

    def _pre_update(self):
        """Prepare the standby slot and optimize the file_table."""
        logger.info("enter local OTA update...")
        with TemporaryDirectory() as _tmp_dir:
            self._can_use_in_place_mode = use_inplace_mode = can_use_in_place_mode(
                dev=self._boot_controller.standby_slot_dev,
                mnt_point=_tmp_dir,
                threshold_in_bytes=int(
                    self._ota_metadata.total_regulars_size
                    * STANDBY_SLOT_USED_SIZE_THRESHOLD
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

    #
    # ------ delta calculation related ------ #
    #

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

    def _calculate_delta(self) -> ResourcesDigestWithSize:
        """Calculate the delta bundle."""
        logger.info("start to calculate delta ...")
        _current_time = int(time.time())

        _fst_db_helper = FileTableDBHelper(self._ota_metadata._fst_db)
        all_resource_digests = ResourcesDigestWithSize.from_iterable(
            _fst_db_helper.select_all_digests_with_size(),
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
            if self._can_use_in_place_mode:
                self._resume_ota_for_inplace_mode_at_delta_cal(all_resource_digests)
                self._resource_dir_on_standby.mkdir(exist_ok=True, parents=True)

                _inplace_mode_params = DeltaGenParams(
                    file_table_db_helper=_fst_db_helper,
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
                    file_table_db_helper=_fst_db_helper,
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

    # ------ end of delta calculation related ------ #

    def _download_delta_resources(self, delta_digests: ResourcesDigestWithSize) -> None:
        """Download all the resources needed for the OTA update."""
        to_download = len(delta_digests)
        to_download_size = sum(delta_digests.values())
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
        try:
            self._download_resources(delta_digests)
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

    def _apply_update(self) -> None:
        """Apply the OTA update to the standby slot."""
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
        self._process_persistents(self._ota_metadata)

        self._preserve_ota_image_meta_at_post_update()
        # NOTE(20250823): secure the resource dir and metadata dir
        os.chmod(self._resource_dir_on_standby, 0o700)
        os.chmod(self._ota_meta_store_on_standby, 0o700)

        self._preserve_client_squashfs()
        self._boot_controller.post_update(self.update_version)

    def _process_persistents(self, ota_metadata: OTAMetadata):
        logger.info("start persist files handling...")
        _handler = PersistFilesHandler(
            src_passwd_file=self._active_slot_mp / "etc/passwd",
            src_group_file=self._active_slot_mp / "etc/group",
            dst_passwd_file=self._standby_slot_mp / "etc/passwd",
            dst_group_file=self._standby_slot_mp / "etc/group",
            src_root=self._active_slot_mp,
            dst_root=self._standby_slot_mp,
        )

        for persiste_entry in ota_metadata.iter_persist_entries():
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

    def _preserve_client_squashfs(self) -> None:
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
        try:
            self._execute_update()
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
