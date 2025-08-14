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

import hashlib
import logging
import shutil
import time
from functools import partial
from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

from ota_image_libs.v1.consts import SUPPORTED_HASH_ALG
from ota_image_libs.v1.image_manifest.schema import ImageIdentifier, OTAReleaseKey

from ota_metadata.utils.cert_store import CACertStore
from ota_metadata.v1 import OTAImageHelper
from otaclient import errors as ota_errors
from otaclient._download_resources import DownloadOTAImageMeta, DownloadResources
from otaclient._status_monitor import (
    OTAUpdatePhaseChangeReport,
    SetUpdateMetaReport,
    StatusReport,
    UpdateProgressReport,
)
from otaclient._types import MultipleECUStatusFlags, UpdatePhase
from otaclient._utils import wait_and_log
from otaclient.boot_control import BootControllerProtocol
from otaclient.configs.cfg import cfg, ecu_info, proxy_info
from otaclient.create_standby import (
    InPlaceDeltaGenFullDiskScan,
    InPlaceDeltaWithBaseFileTable,
    RebuildDeltaGenFullDiskScan,
    RebuildDeltaWithBaseFileTable,
    UpdateStandbySlot,
    can_use_in_place_mode,
)
from otaclient.create_standby.delta_gen import DeltaGenParams
from otaclient.create_standby.resume_ota import ResourceScanner
from otaclient_common import human_readable_size, replace_root
from otaclient_common.cmdhelper import ensure_umount
from otaclient_common.common import ensure_otaproxy_start
from otaclient_common.downloader import DownloaderPool
from otaclient_common.persist_file_handling import PersistFilesHandler
from otaclient_common.retry_task_map import TasksEnsureFailed
from otaclient_common.thread_safe_container import ShardedThreadSafeDict

from ._common import download_exception_handler, parse_cookies

logger = logging.getLogger(__name__)

DEFAULT_STATUS_QUERY_INTERVAL = 1
WAIT_BEFORE_REBOOT = 6
DOWNLOAD_STATS_REPORT_BATCH = 300
DOWNLOAD_REPORT_INTERVAL = 3  # second

OP_CHECK_INTERVAL = 1  # second
HOLD_REQ_HANDLING_ON_ACK_REQUEST = 16  # seconds
WAIT_FOR_OTAPROXY_ONLINE = 3 * 60  # 3mins

STANDBY_SLOT_USED_SIZE_THRESHOLD = 0.8

BASE_METADATA_FOLDER = "base"
"""On standby slot temporary OTA metadata folder(/.ota-meta), `base` folder is to
hold the OTA image metadata of standby slot itself.
"""


class OTAClientError(Exception): ...


class OTAUpdateImpl:
    """The implementation of OTA update logic."""

    def __init__(
        self,
        *,
        version: str,
        ota_release_key: OTAReleaseKey = OTAReleaseKey.dev,
        raw_url_base: str,
        cookies_json: str,
        session_wd: Path,
        ca_chains_store: CACertStore,
        upper_otaproxy: str | None = None,
        boot_controller: BootControllerProtocol,
        ecu_status_flags: MultipleECUStatusFlags,
        status_report_queue: Queue[StatusReport],
        session_id: str,
    ) -> None:
        self.update_version = version
        self.update_start_timestamp = int(time.time())
        self.session_id = session_id
        self._status_report_queue = status_report_queue
        self.image_id = ImageIdentifier(
            release_key=ota_release_key, ecu_id=ecu_info.ecu_id
        )

        # ------ mount session wd as a tmpfs ------ #
        self._session_workdir = session_wd
        session_wd.mkdir(exist_ok=True, parents=True)

        # ------ init updater implementation ------ #
        self.ecu_status_flags = ecu_status_flags
        self._boot_controller = boot_controller

        # ------ define runtime dirs ------ #
        self._resource_dir_on_standby = Path(
            replace_root(
                cfg.OTA_TMP_STORE,
                cfg.CANONICAL_ROOT,
                self._boot_controller.get_standby_slot_path(),
            )
        )
        self._ota_tmp_meta_on_standby = Path(
            replace_root(
                cfg.OTA_TMP_META_STORE,
                cfg.CANONICAL_ROOT,
                self._boot_controller.get_standby_slot_path(),
            )
        )

        # ------ parse OTA image URL ------ #
        _url_base = urlparse(raw_url_base)
        _path = f"{_url_base.path.rstrip('/')}/"
        self.url_base = _url_base._replace(path=_path).geturl()

        # ------ setup downloader with proxy and cookies ------ #
        self._upper_proxy = upper_otaproxy
        self._downloader_pool = DownloaderPool(
            instance_num=cfg.DOWNLOAD_THREADS,
            hash_func=partial(hashlib.new, SUPPORTED_HASH_ALG),
            chunk_size=cfg.CHUNK_SIZE,
            cookies=parse_cookies(cookies_json),
            # NOTE(20221013): check requests document for how to set proxy,
            #                 we only support using http proxy here.
            proxies={"http": upper_otaproxy} if upper_otaproxy else None,
        )

        # ------ setup OTA metadata parser ------ #
        self._image_meta_dir_on_active = Path(cfg.IMAGE_META_DPATH)
        self._image_meta_dir_on_standby = Path(
            replace_root(
                cfg.IMAGE_META_DPATH,
                cfg.CANONICAL_ROOT,
                cfg.STANDBY_SLOT_MNT,
            )
        )
        self._ota_image_helper = OTAImageHelper(
            session_dir=self._session_workdir,
            base_url=self.url_base,
            ca_store=ca_chains_store,
        )
        self._use_inplace_mode = False

        # ------ report INITIALIZING status ------ #
        status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.INITIALIZING,
                    trigger_timestamp=self.update_start_timestamp,
                ),
                session_id=session_id,
            )
        )
        status_report_queue.put_nowait(
            StatusReport(
                payload=SetUpdateMetaReport(
                    update_firmware_version=version,
                ),
                session_id=session_id,
            )
        )

    def _download_and_parse_metadata(
        self, _image_id: ImageIdentifier, _meta_downloader: DownloadOTAImageMeta
    ) -> None:
        for _fut in _meta_downloader.download_ota_image_meta(_image_id):
            download_exception_handler(_fut)

    def _download_resources(self, _resource_downloader: DownloadResources) -> None:
        _next_commit_before = 0
        _merged_payload = UpdateProgressReport(
            operation=UpdateProgressReport.Type.DOWNLOAD_REMOTE_COPY
        )
        for _fut in _resource_downloader.download_resources():
            _now = time.perf_counter()
            if download_exception_handler(_fut):
                _merged_payload.processed_file_num += 1
                _download_res = _fut.result()

                _merged_payload.processed_file_size += _download_res.download_size
                _merged_payload.errors += _download_res.retry_count
                _merged_payload.downloaded_bytes += _download_res.traffic_on_wire
            else:
                _merged_payload.errors += 1

            if _now > _next_commit_before:
                _next_commit_before = _now + DOWNLOAD_REPORT_INTERVAL
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
            StatusReport(payload=_merged_payload, session_id=self.session_id)
        )

    def _process_persistents(self):
        logger.info("start persist files handling...")
        _persist_list = self._ota_image_helper.get_persistents_list()
        if not _persist_list:
            logger.info("this OTA image payload doesn't configure persist files")
            return

        standby_slot_mp = Path(cfg.STANDBY_SLOT_MNT)

        _handler = PersistFilesHandler(
            src_passwd_file=Path(cfg.PASSWD_FPATH),
            src_group_file=Path(cfg.GROUP_FPATH),
            dst_passwd_file=Path(standby_slot_mp / "etc/passwd"),
            dst_group_file=Path(standby_slot_mp / "etc/group"),
            src_root=cfg.ACTIVE_SLOT_MNT,
            dst_root=cfg.STANDBY_SLOT_MNT,
        )

        for persiste_entry in _persist_list:
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

    def _wait_for_otaproxy(self, _upper_proxy: str):
        logger.info(
            f"use {_upper_proxy} for local OTA update, "
            f"wait for otaproxy@{_upper_proxy} online..."
        )
        # NOTE: will raise a built-in ConnnectionError at timeout
        ensure_otaproxy_start(
            _upper_proxy,
            probing_timeout=WAIT_FOR_OTAPROXY_ONLINE,
        )

    def _prepare_ota_image_meta(self):
        try:
            self._status_report_queue.put_nowait(
                StatusReport(
                    payload=OTAUpdatePhaseChangeReport(
                        new_update_phase=UpdatePhase.PROCESSING_METADATA,
                        trigger_timestamp=int(time.time()),
                    ),
                    session_id=self.session_id,
                )
            )
            _ota_image_meta_download_helper = DownloadOTAImageMeta(
                self._ota_image_helper,
                downloader_pool=self._downloader_pool,
                max_concurrent=cfg.MAX_CONCURRENT_DOWNLOAD_TASKS,
                download_inactive_timeout=cfg.DOWNLOAD_INACTIVE_TIMEOUT,
            )
            self._download_and_parse_metadata(
                self.image_id,
                _ota_image_meta_download_helper,
            )

            _image_config = self._ota_image_helper.image_config
            assert _image_config

            _sys_image_size = _image_config.labels.sys_image_size or 0
            logger.info(
                "ota_metadata parsed finished: \n"
                f"total_regulars_num: {_image_config.sys_image_regular_files_count} \n"
                f"total_regulars_size: {_sys_image_size}"
            )

            self._status_report_queue.put_nowait(
                StatusReport(
                    payload=SetUpdateMetaReport(
                        image_file_entries=_image_config.sys_image_regular_files_count,
                        image_size_uncompressed=_sys_image_size,
                        metadata_downloaded_bytes=self._downloader_pool.total_downloaded_bytes,
                    ),
                    session_id=self.session_id,
                )
            )
        except ota_errors.OTAError:
            raise  # raise top-level OTAError as it
        # TODO: errors for metadata parsing
        except Exception as e:
            _err_msg = f"failed to prepare ota metafiles: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.OTAMetaDownloadFailed(_err_msg, module=__name__) from e

    def _pre_update(self):
        _image_config = self._ota_image_helper.image_config
        assert _image_config
        with TemporaryDirectory() as _tmp_dir:
            self._use_inplace_mode = use_inplace_mode = can_use_in_place_mode(
                dev=self._boot_controller.standby_slot_dev,
                mnt_point=_tmp_dir,
                threshold_in_bytes=int(
                    _image_config.sys_image_unique_file_entries_size
                    * STANDBY_SLOT_USED_SIZE_THRESHOLD
                ),
            )
        logger.info(
            f"check if we can use in-place mode to update standby slot: {use_inplace_mode}"
        )

        self._boot_controller.pre_update(
            # NOTE: this option is deprecated and not used by bootcontroller
            # TODO:(20250613) when standby_as_ref is set, skip mounting active slot.
            #       we cannot do this for now, as some boot controller impl still refer to
            #       active_slot mounts.
            standby_as_ref=use_inplace_mode,
            erase_standby=not use_inplace_mode,
        )

    def _in_update_delta_calculate(self) -> ShardedThreadSafeDict[bytes, int]:
        _fst_helper = self._ota_image_helper.file_table_helper

        all_resource_digests = ShardedThreadSafeDict[bytes, int].from_iterable(
            _fst_helper.select_all_digests_with_size(),
        )
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.CALCULATING_DELTA,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.session_id,
            )
        )
        if self._use_inplace_mode and self._resource_dir_on_standby.is_dir():
            logger.info(
                "OTA resource dir found on standby slot, possible an interrupted OTA. \n"
                "Try to resume previous OTA delta calculation progress ..."
            )
            ResourceScanner(
                all_resource_digests=all_resource_digests,
                resource_dir=self._resource_dir_on_standby,
                status_report_queue=self._status_report_queue,
                session_id=self.session_id,
            ).resume_ota()
            logger.info("finish resuming previous OTA progress")

        # prepare the tmp storage on standby slot after boot_controller.pre_update finished
        self._resource_dir_on_standby.mkdir(exist_ok=True, parents=True)
        self._ota_tmp_meta_on_standby.mkdir(exist_ok=True, parents=True)

        # NOTE(20250529): first save it to /.ota-meta, and then save it to the actual
        #                 destination folder.
        logger.info("save the OTA image file_table to standby slot ...")
        try:
            _fst_helper.save_fstable(self._ota_tmp_meta_on_standby)
        except Exception as e:
            logger.error(
                f"failed to save OTA image file_table to {self._ota_tmp_meta_on_standby=}: {e!r}"
            )

        base_meta_dir_on_standby_slot = None
        try:
            if self._use_inplace_mode:
                # try to use base file_table from standby slot itself
                base_meta_dir_on_standby_slot = (
                    self._ota_tmp_meta_on_standby / BASE_METADATA_FOLDER
                )

                # NOTE: the file_table file in /opt/ota/image-meta MUST be prepared by otaclient,
                #       it is not included in the OTA image, thus also not in file_table.
                verified_base_db = None
                # NOTE: if the previous OTA is interrupted, and it is base file_table assisted,
                #       try to keep using the file_table.
                if base_meta_dir_on_standby_slot.is_dir():
                    verified_base_db = _fst_helper.find_saved_fstable(
                        base_meta_dir_on_standby_slot
                    )
                else:
                    shutil.rmtree(base_meta_dir_on_standby_slot, ignore_errors=True)
                    if self._image_meta_dir_on_standby.is_dir():
                        shutil.move(
                            self._image_meta_dir_on_standby,
                            base_meta_dir_on_standby_slot,
                        )
                        verified_base_db = _fst_helper.find_saved_fstable(
                            base_meta_dir_on_standby_slot
                        )

                _inplace_mode_params = DeltaGenParams(
                    file_table_db_helper=_fst_helper,
                    all_resource_digests=all_resource_digests,
                    delta_src=Path(cfg.STANDBY_SLOT_MNT),
                    copy_dst=self._resource_dir_on_standby,
                    status_report_queue=self._status_report_queue,
                    session_id=self.session_id,
                )

                if verified_base_db:
                    logger.info("use in-place mode with base file table assist ...")
                    InPlaceDeltaWithBaseFileTable(**_inplace_mode_params).process_slot(
                        str(verified_base_db)
                    )
                else:
                    logger.info("use in-place mode with full scanning ...")
                    InPlaceDeltaGenFullDiskScan(**_inplace_mode_params).process_slot()
            else:
                _rebuild_mode_params = DeltaGenParams(
                    file_table_db_helper=_fst_helper,
                    all_resource_digests=all_resource_digests,
                    delta_src=Path(cfg.ACTIVE_SLOT_MNT),
                    copy_dst=self._resource_dir_on_standby,
                    status_report_queue=self._status_report_queue,
                    session_id=self.session_id,
                )

                verified_base_db = _fst_helper.find_saved_fstable(
                    self._image_meta_dir_on_active
                )
                if verified_base_db:
                    logger.info("use rebuild mode with base file table assist ...")
                    RebuildDeltaWithBaseFileTable(**_rebuild_mode_params).process_slot(
                        str(verified_base_db)
                    )
                else:
                    logger.info("use rebuild mode with full scanning ...")
                    RebuildDeltaGenFullDiskScan(**_rebuild_mode_params).process_slot()

            # on success return the resources we need to download
            return all_resource_digests
        except Exception as e:
            _err_msg = f"failed to generate delta: {e!r}"
            logger.exception(_err_msg)
            raise ota_errors.UpdateDeltaGenerationFailed(
                _err_msg, module=__name__
            ) from e
        finally:
            # we don't need the copy of base file table after delta calculation
            if base_meta_dir_on_standby_slot and base_meta_dir_on_standby_slot.is_dir():
                shutil.rmtree(base_meta_dir_on_standby_slot, ignore_errors=True)

    def _in_update_download_resources(
        self, resources_to_download: ShardedThreadSafeDict[bytes, int]
    ):
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.DOWNLOADING_OTA_FILES,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.session_id,
            )
        )
        # NOTE(20240705): download_files raises OTA Error directly, no need to capture exc here
        _rst_helper = self._ota_image_helper.resource_table_helper
        try:
            _resource_downloader = DownloadResources(
                _rst_helper,
                blob_storage_base_url=self._ota_image_helper.resource_url,
                resource_dir=self._resource_dir_on_standby,
                downloader_pool=self._downloader_pool,
                max_concurrent=cfg.MAX_CONCURRENT_DOWNLOAD_TASKS,
                download_inactive_timeout=cfg.DOWNLOAD_INACTIVE_TIMEOUT,
                resources_to_download=resources_to_download,
            )
            self._download_resources(_resource_downloader)
        except TasksEnsureFailed:
            _err_msg = (
                "download aborted due to download stalls longer than "
                f"{cfg.DOWNLOAD_INACTIVE_TIMEOUT}, or otaclient process is terminated, abort OTA"
            )
            logger.error(_err_msg)
            raise ota_errors.NetworkError(_err_msg, module=__name__) from None

        # NOTE: after this point, we don't need downloader anymore
        # release the downloader instances
        self._downloader_pool.shutdown()

    def _in_update_apply_changes(self):
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.APPLYING_UPDATE,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.session_id,
            )
        )

        _fst_helper = self._ota_image_helper.file_table_helper
        try:
            standby_slot_creator = UpdateStandbySlot(
                file_table_db_helper=_fst_helper,
                standby_slot_mount_point=cfg.STANDBY_SLOT_MNT,
                status_report_queue=self._status_report_queue,
                session_id=self.session_id,
                resource_dir=self._resource_dir_on_standby,
            )
            standby_slot_creator.update_slot()
        except Exception as e:
            raise ota_errors.ApplyOTAUpdateFailed(
                f"failed to apply update to standby slot: {e!r}", module=__name__
            ) from e

    def _post_update(self):
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.PROCESSING_POSTUPDATE,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.session_id,
            )
        )
        # NOTE(20240219): move persist file handling here
        self._process_persistents()

        # save the OTA metadata to the actual location after
        #   standby slot rootfs updated
        ota_metadata_save_dst = Path(
            replace_root(
                cfg.IMAGE_META_DPATH,
                cfg.CANONICAL_ROOT,
                self._boot_controller.get_standby_slot_path(),
            )
        )
        ota_metadata_save_dst.mkdir(exist_ok=True, parents=True)
        shutil.rmtree(ota_metadata_save_dst, ignore_errors=True)
        shutil.move(self._ota_tmp_meta_on_standby, ota_metadata_save_dst)

        self._boot_controller.post_update(self.update_version)

    def _finalizing_update(self):
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.FINALIZING_UPDATE,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.session_id,
            )
        )

        if proxy_info.enable_local_ota_proxy:
            wait_and_log(
                check_flag=self.ecu_status_flags.any_child_ecu_in_update.is_set,
                check_for=False,
                message="permit reboot flag",
                log_func=logger.info,
            )

        logger.info(f"device will reboot in {WAIT_BEFORE_REBOOT} seconds!")
        time.sleep(WAIT_BEFORE_REBOOT)
        self._boot_controller.finalizing_update()

    # entrypoint

    def execute(self) -> None:
        """Main entry for executing local OTA update.

        Handles OTA failure and logging/finalizing on failure.
        """
        logger.info(f"execute local update({ecu_info.ecu_id=}): {self.update_version=}")
        try:
            if _upper_proxy := self._upper_proxy:
                self._wait_for_otaproxy(_upper_proxy)

            # ------ init, processing metadata ------ #
            logger.info("verify and download OTA image metadata ...")
            self._prepare_ota_image_meta()

            # ------ pre-update ------ #
            logger.info("enter local OTA update...")
            self._pre_update()

            # ------ in-update: calculate delta ------ #
            logger.info("start to calculate and prepare delta...")
            all_resource_digests = self._in_update_delta_calculate()

            download_files_num = len(all_resource_digests)
            download_files_size = sum(all_resource_digests.values())
            self._status_report_queue.put_nowait(
                StatusReport(
                    payload=SetUpdateMetaReport(
                        total_download_files_num=download_files_num,
                        total_download_files_size=download_files_size,
                    ),
                    session_id=self.session_id,
                )
            )
            logger.info(
                f"delta calculation finished: \n"
                f"download_list len: {download_files_num} \n"
                f"sum of original size of all resources to be downloaded: {human_readable_size(download_files_size)}"
            )

            # ------ in-update: download resources ------ #
            logger.info("start to download resources ...")
            self._in_update_download_resources(all_resource_digests)

            # ------ apply update ------ #
            logger.info("start to apply changes to standby slot...")
            self._in_update_apply_changes()

            # ------ post-update ------ #
            logger.info("enter post update phase...")
            self._post_update()

            # ------ finalizing update ------ #
            logger.info("local update finished, wait on all subecs...")
            self._finalizing_update()
            # NOTE(20250813): preserving the resource dir on standby slot,
            #                 so that next OTA can direclty utilize the resources
            #                 for OTA update, effectively making delta calculation
            #                 time cost much smaller.
            # shutil.rmtree(self._resource_dir_on_standby, ignore_errors=True)
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
