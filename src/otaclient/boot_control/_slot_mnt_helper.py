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
"""Helper for mounting/umount slots during OTA."""

from __future__ import annotations

import atexit
import logging
import shutil
from functools import partial
from pathlib import Path

from otaclient.configs.cfg import cfg
from otaclient_common import _env, cmdhelper
from otaclient_common._typing import StrOrPath

logger = logging.getLogger(__name__)


class SlotMountHelper:  # pragma: no cover
    """Helper class that provides methods for mounting slots."""

    def __init__(
        self,
        *,
        standby_slot_dev: StrOrPath,
        standby_slot_mount_point: StrOrPath,
        active_rootfs: StrOrPath,
        active_slot_mount_point: StrOrPath,
    ) -> None:
        self.standby_slot_dev = str(standby_slot_dev)
        self.active_rootfs = str(active_rootfs)

        self.standby_slot_mount_point = Path(standby_slot_mount_point)
        self.active_slot_mount_point = Path(active_slot_mount_point)

        # ensure the each mount points being umounted at termination
        # if the dynamic client is running, active slot should be mounted before chroot
        if not _env.is_dynamic_client_running():
            atexit.register(
                partial(
                    cmdhelper.ensure_umount,
                    self.active_slot_mount_point,
                    ignore_error=True,
                )
            )
        atexit.register(
            partial(
                cmdhelper.ensure_umount,
                self.standby_slot_mount_point,
                ignore_error=True,
                max_retry=3,
            )
        )

    def mount_standby(self) -> None:
        """Mount standby slot dev rw to <standby_slot_mount_point>.

        Raises:
            CalledProcessedError on the last failed attemp.
        """
        logger.debug("mount standby slot rootfs dev...")
        cmdhelper.ensure_mointpoint(self.standby_slot_mount_point, ignore_error=True)
        cmdhelper.ensure_umount(self.standby_slot_dev, ignore_error=False)

        cmdhelper.ensure_mount(
            target=self.standby_slot_dev,
            mnt_point=self.standby_slot_mount_point,
            mount_func=cmdhelper.mount_rw,
            raise_exception=True,
        )

    def mount_active(self) -> None:
        """Mount current active rootfs ready-only.

        Raises:
            CalledProcessedError on the last failed attemp.
        """
        logger.debug("mount active slot rootfs dev...")

        # if the dynamic client is running, active slot should be mounted before chroot
        if _env.is_dynamic_client_running():
            return

        cmdhelper.ensure_mointpoint(self.active_slot_mount_point, ignore_error=True)
        cmdhelper.ensure_mount(
            target=self.active_rootfs,
            mnt_point=self.active_slot_mount_point,
            mount_func=cmdhelper.bind_mount_ro,
            raise_exception=True,
        )

    def preserve_ota_folder_to_standby(self):
        """Copy the /boot/ota folder to standby slot to preserve it.

        /boot/ota folder contains the ota setting for this device,
        so we should preserve it for each slot, accross each update.
        """
        logger.debug("copy /boot/ota from active to standby.")
        try:
            _src = self.active_slot_mount_point / Path(cfg.OTA_DPATH).relative_to("/")
            _dst = self.standby_slot_mount_point / Path(cfg.OTA_DPATH).relative_to("/")
            shutil.copytree(_src, _dst, dirs_exist_ok=True)
        except Exception as e:
            raise ValueError(
                f"failed to copy /boot/ota from active to standby: {e!r}"
            ) from e

    def prepare_standby_dev(
        self,
        *,
        erase_standby: bool = False,
        fslabel: str | None = None,
    ) -> None:
        _target_dev = self.standby_slot_dev
        cmdhelper.ensure_umount(_target_dev, ignore_error=True)
        if _env.is_dynamic_client_running():
            logger.info(
                "running as dynamic app, also umount standby from host mnt ns ..."
            )
            cmdhelper.ensure_umount_from_host(_target_dev, ignore_error=False)

        if erase_standby:
            return cmdhelper.mkfs_ext4(_target_dev, fslabel=fslabel)
        if fslabel:
            cmdhelper.set_ext4_fslabel(_target_dev, fslabel=fslabel)

    def umount_all(self, *, ignore_error: bool = True):
        logger.debug("unmount standby slot and active slot mount point...")

        # if the dynamic client is running, active slot should be mounted before chroot
        if not _env.is_dynamic_client_running():
            cmdhelper.ensure_umount(
                self.active_slot_mount_point, ignore_error=ignore_error
            )
        cmdhelper.ensure_umount(
            self.standby_slot_mount_point, ignore_error=ignore_error
        )
