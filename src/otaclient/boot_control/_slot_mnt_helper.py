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

import logging
import shutil
from pathlib import Path
from subprocess import CalledProcessError
from time import sleep
from typing import Optional

from otaclient.boot_control._common import CMDHelperFuncs
from otaclient.configs.cfg import cfg
from otaclient_common import replace_root
from otaclient_common.typing import StrOrPath

logger = logging.getLogger(__name__)

MAX_RETRY_COUNT = 6
RETRY_INTERVAL = 2


def ensure_mount(
    target: StrOrPath, mnt_point: StrOrPath, *, mount_func, raise_exception: bool = True
) -> None:
    """Ensure the <target> mounted on <mnt_point> by our best.

    Raises:
        If <raise_exception> is True, raises the last failed attemp's CalledProcessError.
    """
    for _retry in range(MAX_RETRY_COUNT + 1):
        try:
            mount_func(target=target, mount_point=mnt_point)
            CMDHelperFuncs.is_target_mounted(target, raise_exception=True)
            return
        except CalledProcessError as e:
            logger.error(f"retry#{_retry} failed to mout standby slot: {e!r}")
            logger.error(f"{e.stderr=}\n{e.stdout=}")

            if _retry >= MAX_RETRY_COUNT:
                logger.error("exceed max retry count on mounting, abort")
                if raise_exception:
                    raise
                return

            sleep(RETRY_INTERVAL)
            continue


def ensure_umount(mnt_point: StrOrPath, *, ignore_error: bool) -> None:
    """Try to umount the <mnt_point> at our best.

    Raises:
        If <ignore_error> is False, raises the last failed attemp's CalledProcessError.
    """
    if not CMDHelperFuncs.is_target_mounted(mnt_point):
        return

    for _retry in range(MAX_RETRY_COUNT + 1):
        try:
            CMDHelperFuncs.umount(mnt_point, raise_exception=True)
            break
        except CalledProcessError as e:
            logger.warning(f"retry#{_retry} failed to umount standby slot: {e!r}")
            logger.warning(f"{e.stderr}\n{e.stdout}")

            if _retry >= MAX_RETRY_COUNT:
                logger.error(f"reached max retry on umounting {mnt_point}, abort")
                if not ignore_error:
                    raise
                return

            sleep(RETRY_INTERVAL)
            continue


def ensure_mointpoint(mnt_point: Path) -> None:
    """Ensure the <mnt_point> exists, has no mount on it and ready for mount.

    If the <mnt_point> is valid, but we failed to umount any previous mounts on it,
        we still keep use the mountpoint as later mount will override the previous one.
    """
    if not mnt_point.is_dir():
        mnt_point.unlink(missing_ok=True)

    if not mnt_point.exists():
        mnt_point.mkdir(exist_ok=True, parents=True)
        return

    try:
        ensure_umount(mnt_point, ignore_error=False)
    except Exception:
        logger.warning(f"still use {mnt_point} and override the previous mount")


class SlotMountHelper:
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

        # standby slot /boot dir
        # NOTE(20230907): this will always be <standby_slot_mp>/boot,
        #                 in the future this attribute will not be used by
        #                 standby slot creater.
        self.standby_boot_dir = Path(
            replace_root(
                cfg.BOOT_DPATH, cfg.CANONICAL_ROOT, self.standby_slot_mount_point
            )
        )

    def mount_standby(self) -> None:
        """Mount standby slot dev to <standby_slot_mount_point>.

        Raises:
            CalledProcessedError on the last failed attemp.
        """
        logger.debug("mount standby slot rootfs dev...")
        ensure_mointpoint(self.standby_slot_mount_point)
        ensure_mount(
            target=self.standby_slot_dev,
            mnt_point=self.standby_slot_mount_point,
            mount_func=CMDHelperFuncs.mount_rw,
        )

    def mount_active(self) -> None:
        """Mount current active rootfs ready-only.

        Raises:
            CalledProcessedError on the last failed attemp.
        """
        logger.debug("mount active slot rootfs dev...")
        ensure_mointpoint(self.active_slot_mount_point)
        ensure_mount(
            target=self.active_rootfs,
            mnt_point=self.active_slot_mount_point,
            mount_func=CMDHelperFuncs.bind_mount_ro,
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
        fslabel: Optional[str] = None,
    ) -> None:
        CMDHelperFuncs.umount(self.standby_slot_dev, raise_exception=False)
        if erase_standby:
            return CMDHelperFuncs.mkfs_ext4(self.standby_slot_dev, fslabel=fslabel)

        # TODO: in the future if in-place update mode is implemented, do a
        #   fschck over the standby slot file system.
        if fslabel:
            CMDHelperFuncs.set_ext4_fslabel(self.standby_slot_dev, fslabel=fslabel)

    def umount_all(self, *, ignore_error: bool = True):
        logger.debug("unmount standby slot and active slot mount point...")
        ensure_umount(self.active_slot_mount_point, ignore_error=ignore_error)
        ensure_umount(self.standby_slot_mount_point, ignore_error=ignore_error)
