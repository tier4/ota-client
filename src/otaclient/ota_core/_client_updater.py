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
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ota_image_libs.v1.image_manifest.schema import ImageIdentifier
from typing_extensions import Unpack

from ota_metadata.utils.cert_store import CAChainStore, CAStoreMap
from otaclient import __version__
from otaclient import errors as ota_errors
from otaclient._status_monitor import OTAUpdatePhaseChangeReport, StatusReport
from otaclient._types import (
    ClientUpdateControlFlags,
    MultipleECUStatusFlags,
    UpdatePhase,
)
from otaclient._utils import wait_and_log
from otaclient.client_package import OTAClientPackageDownloader
from otaclient.configs.cfg import cfg, ecu_info
from otaclient_common import _env, cmdhelper
from otaclient_common._env import get_otaclient_squashfs_download_dst
from otaclient_common._typing import StrOrPath
from otaclient_common.cmdhelper import ensure_umount

from ._common import download_exception_handler
from ._updater_base import (
    LegacyOTAImageSupportMixin,
    OTAImageV1SupportMixin,
    OTAUpdateInitializer,
    OTAUpdateInterfaceArgs,
)

logger = logging.getLogger(__name__)


class _ClientUpdateFinishMixin:
    """The steps both client updaters share once the client's package is on disk."""

    if TYPE_CHECKING:
        _standby_slot_dev: StrOrPath
        client_update_control_flags: ClientUpdateControlFlags
        ecu_status_flags: MultipleECUStatusFlags

    def _wait_sub_ecus(self) -> None:
        logger.info("wait for all sub-ECU to finish...")
        # wait for all sub-ECU to finish OTA update
        result = wait_and_log(
            check_flag=self.ecu_status_flags.any_child_ecu_in_update.is_set,
            check_for=False,
            message="client updating in sub ecus",
            log_func=logger.info,
            timeout=cfg.CLIENT_UPDATE_TIMEOUT,
        )
        if result is False:
            logger.warning("sub-ECU client was aborted, skip waiting for sub-ecus")

    def _notify_data_ready(self) -> None:
        """Notify the main process that the client package is ready."""
        logger.info("notify main process that the client package is ready..")
        cmdhelper.ensure_umount(self._standby_slot_dev, ignore_error=False)
        if _env.is_dynamic_client_running():
            logger.info(
                "ensure standby_slot dev is umounted before launching dynamic otaclient app ..."
            )
            cmdhelper.ensure_umount_from_host(
                self._standby_slot_dev, ignore_error=False
            )

        self.client_update_control_flags.notify_data_ready_event.set()


class OTAClientUpdater(
    _ClientUpdateFinishMixin, LegacyOTAImageSupportMixin, OTAUpdateInitializer
):
    """The implementation of OTA client update logic."""

    def __init__(
        self,
        *,
        standby_slot_dev: StrOrPath,
        ca_chains_store: CAChainStore,
        client_update_control_flags: ClientUpdateControlFlags,
        **kwargs: Unpack[OTAUpdateInterfaceArgs],
    ) -> None:
        # ------ init base class ------ #
        OTAUpdateInitializer.__init__(self, **kwargs)
        self._standby_slot_dev = standby_slot_dev
        self.setup_ota_image_support(ca_chains_store=ca_chains_store)

        # --- Event flag to control client update ---- #
        self.client_update_control_flags = client_update_control_flags

        # ------ setup OTA client package parser ------ #
        self._ota_client_package = OTAClientPackageDownloader(
            base_url=self.url_base,
            ota_metadata=self._ota_metadata,
            session_dir=self._session_workdir,
            package_install_dir=cfg.OTACLIENT_INSTALLATION_RELEASE,
            squashfs_file=get_otaclient_squashfs_download_dst(),
        )

    def _execute_client_update(self):
        """Implementation of OTA updating."""
        logger.info(
            f"execute local update({ecu_info.ecu_id=}): "
            f"{self.update_version=}, {self.release_name=}, {self.release_id=}, {self.image_id=}"
        )

        try:
            self._process_metadata(only_metadata_verification=True)
            self._download_client_package_resources()
            self._wait_sub_ecus()
            if self._is_same_client_package_version():
                # to notify the status report after reboot
                _err_msg = "client package version is the same, skip client update"
                logger.info(_err_msg)
                raise ota_errors.ClientUpdateSameVersions(_err_msg, module=__name__)
            else:
                self._copy_client_package()
                self._notify_data_ready()
        except ota_errors.ClientUpdateSameVersions:
            raise
        except Exception as e:
            _err_msg = f"client update failed: {e!r}"
            logger.exception(_err_msg)
            raise ota_errors.ClientUpdateFailed(_err_msg, module=__name__) from e

    def _download_client_package_resources(self) -> None:
        """Download OTA client."""
        logger.info("start to download client manifest and package...")
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.DOWNLOADING_OTA_CLIENT,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.session_id,
            )
        )

        _condition = threading.Condition()
        try:
            for _fut in self._download_helper.download_meta_files(
                self._ota_client_package.download_client_package(_condition),
                condition=_condition,
            ):
                download_exception_handler(_fut)
        except Exception as e:
            _err_msg = f"failed to download otaclient package: {e!r}"
            logger.exception(_err_msg)
            raise ota_errors.OTAClientPackageDownloadFailed(module=__name__) from e
        finally:
            self._downloader_pool.shutdown()

    def _is_same_client_package_version(self) -> bool:
        return self._ota_client_package.is_same_client_package_version()

    def _copy_client_package(self) -> None:
        self._ota_client_package.copy_client_package()

    # API

    def execute(self) -> None:
        """Main entry for executing local OTA client update."""
        try:
            self._execute_client_update()
        finally:
            ensure_umount(self._session_workdir, ignore_error=True)
            shutil.rmtree(self._session_workdir, ignore_errors=True)


OTACLIENT_SQUASHFS_NAME = "otaclient.squashfs"


class OTAClientUpdaterForOTAImageV1(
    _ClientUpdateFinishMixin, OTAImageV1SupportMixin, OTAUpdateInitializer
):
    """Updating otaclient itself from an OTA image version 1.

    The same steps as the legacy client update, with the client's package found through
    the signed index as an artifact of the image. Kept beside the legacy client updater:
    the two formats find the package in different ways, and the legacy path is what the
    fleet runs today.
    """

    def __init__(
        self,
        *,
        standby_slot_dev: StrOrPath,
        ca_store: CAStoreMap,
        image_identifier: ImageIdentifier,
        client_update_control_flags: ClientUpdateControlFlags,
        **kwargs: Unpack[OTAUpdateInterfaceArgs],
    ) -> None:
        OTAUpdateInitializer.__init__(self, **kwargs)
        self._standby_slot_dev = standby_slot_dev
        self.client_update_control_flags = client_update_control_flags
        self.setup_ota_image_support(
            ca_store=ca_store, image_identifier=image_identifier
        )

    def _download_client_package(self, dst: Path) -> None:
        logger.info("start to download the otaclient release package...")
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.DOWNLOADING_OTA_CLIENT,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.session_id,
            )
        )

        _condition = threading.Condition()
        try:
            for _fut in self._download_helper.download_meta_files(
                self._ota_image_helper.select_otaclient_package(
                    dst, self.update_version, condition=_condition
                ),
                condition=_condition,
            ):
                download_exception_handler(_fut)
        except Exception as e:
            _err_msg = f"failed to download otaclient package: {e!r}"
            logger.exception(_err_msg)
            raise ota_errors.OTAClientPackageDownloadFailed(module=__name__) from e
        finally:
            self._downloader_pool.shutdown()

    def _execute_client_update(self) -> None:
        logger.info(
            f"execute local client update({ecu_info.ecu_id=}): "
            f"{self.update_version=}, {self.image_id=}"
        )
        try:
            if self.update_version == __version__:
                raise ota_errors.ClientUpdateSameVersions(
                    f"the running client is already {__version__}, skip client update",
                    module=__name__,
                )

            self._process_metadata(only_metadata_verification=True)
            _downloaded = self._session_workdir / OTACLIENT_SQUASHFS_NAME
            self._download_client_package(_downloaded)
            if not _downloaded.is_file():
                # select_otaclient_package says so by downloading nothing: no package
                # manifest in the image, or none for this version and architecture.
                raise ota_errors.ClientUpdateFailed(
                    f"the OTA image carries no otaclient package for "
                    f"{self.update_version} on this architecture",
                    module=__name__,
                )
            self._wait_sub_ecus()

            # NOTE: the path is a constant on the running root; an image with a
            #       read-only root links it to somewhere writable.
            _dst = Path(get_otaclient_squashfs_download_dst())
            os.makedirs(_dst.parent, exist_ok=True)
            shutil.copy(_downloaded, _dst)
            logger.info(f"the client to run is at {_dst}")
            self._notify_data_ready()
        except ota_errors.OTAError:
            raise
        except Exception as e:
            _err_msg = f"client update failed: {e!r}"
            logger.exception(_err_msg)
            raise ota_errors.ClientUpdateFailed(_err_msg, module=__name__) from e

    # API

    def execute(self) -> None:
        """Main entry for executing local OTA client update."""
        try:
            self._execute_client_update()
        finally:
            ensure_umount(self._session_workdir, ignore_error=True)
            shutil.rmtree(self._session_workdir, ignore_errors=True)
