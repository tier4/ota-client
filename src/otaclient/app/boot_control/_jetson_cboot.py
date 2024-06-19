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
"""Boot control implementation for NVIDIA Jetson device boots with cboot.

Supports BSP version < R34.
"""


from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Generator, Optional

from otaclient_common import cmdhelper
from otaclient.app import errors as ota_errors
from otaclient.app.boot_control._common import OTAStatusFilesControl, SlotMountHelper
from otaclient.app.boot_control._jetson_common import (
    FirmwareBSPVersionControl,
    NVBootctrlCommon,
    NVBootctrlTarget,
    SlotID,
    copy_standby_slot_boot_to_internal_emmc,
    parse_bsp_version,
    preserve_ota_config_files_to_standby,
    update_standby_slot_extlinux_cfg,
)
from otaclient.app.boot_control.configs import cboot_cfg as boot_cfg
from otaclient.app.boot_control.protocol import BootControllerProtocol
from otaclient.app.configs import config as cfg
from otaclient_api.v2 import types as api_types
from otaclient_common.common import subprocess_run_wrapper

logger = logging.getLogger(__name__)


class JetsonCBootContrlError(Exception):
    """Exception types for covering jetson-cboot related errors."""


class _NVBootctrl(NVBootctrlCommon):
    """Helper for calling nvbootctrl commands.

    For BSP version < R34.
    Without -t option, the target will be bootloader by default.
    """

    @classmethod
    def mark_boot_successful(
        cls, slot_id: SlotID, *, target: Optional[NVBootctrlTarget] = None
    ) -> None:
        """Mark current slot as GOOD."""
        cmd = "mark-boot-successful"
        cls._nvbootctrl(cmd, slot_id, check_output=False, target=target)

    @classmethod
    def set_slot_as_unbootable(
        cls, slot_id: SlotID, *, target: Optional[NVBootctrlTarget] = None
    ) -> None:
        """Mark SLOT as invalid."""
        cmd = "set-slot-as-unbootable"
        return cls._nvbootctrl(cmd, SlotID(slot_id), check_output=False, target=target)

    @classmethod
    def is_unified_enabled(cls) -> bool | None:
        """Returns 0 only if unified a/b is enabled.

        NOTE: this command is available after BSP R32.6.1.

        Meaning of return code:
            - 0 if both unified A/B and rootfs A/B are enabled
            - 69 if both unified A/B and rootfs A/B are disabled
            - 70 if rootfs A/B is enabled and unified A/B is disabled

        Returns:
            True for both unified A/B and rootfs A/B are enbaled,
                False for unified A/B disabled but rootfs A/B enabled,
                None for both disabled.
        """
        cmd = "is-unified-enabled"
        try:
            cls._nvbootctrl(cmd, check_output=False)
            return True
        except subprocess.CalledProcessError as e:
            if e.returncode == 70:
                return False
            elif e.returncode == 69:
                return
            raise ValueError(f"{cmd} returns unexpected result: {e.returncode=}, {e!r}")


class NVUpdateEngine:
    """Firmware update implementation using nv_update_engine."""

    NV_UPDATE_ENGINE = "nv_update_engine"

    @classmethod
    def _nv_update_engine(cls, payload: Path | str):
        """nv_update_engine apply BUP, non unified_ab version."""
        # fmt: off
        cmd = [
            cls.NV_UPDATE_ENGINE,
            "-i", "bl",
            "--payload", str(payload),
            "--no-reboot",
        ]
        # fmt: on
        res = subprocess_run_wrapper(cmd, check=True, check_output=True)
        logger.info(
            (
                f"apply BUP {payload=}: \n"
                f"stdout: {res.stdout.decode()}\n"
                f"stderr: {res.stderr.decode()}"
            )
        )

    @classmethod
    def _nv_update_engine_unified_ab(cls, payload: Path | str):
        """nv_update_engine apply BUP, unified_ab version."""
        # fmt: off
        cmd = [
            cls.NV_UPDATE_ENGINE,
            "-i", "bl-only",
            "--payload", str(payload),
        ]
        # fmt: on
        res = subprocess_run_wrapper(cmd, check=True, check_output=True)
        logger.info(
            (
                f"apply BUP {payload=} with unified A/B: \n"
                f"stdout: {res.stdout.decode()}\n"
                f"stderr: {res.stderr.decode()}"
            )
        )

    @classmethod
    def apply_firmware_update(cls, payload: Path | str, *, unified_ab: bool) -> None:
        if unified_ab:
            return cls._nv_update_engine_unified_ab(payload)
        return cls._nv_update_engine(payload)

    @classmethod
    def verify_update(cls) -> subprocess.CompletedProcess[bytes]:
        """Dump the nv_update_engine update verification.

        NOTE: no exception will be raised, the caller MUST check the
            call result by themselves.

        Returns:
            A CompletedProcess object with the call result.
        """
        cmd = [cls.NV_UPDATE_ENGINE, "--verify"]
        return subprocess_run_wrapper(cmd, check=False, check_output=True)


class _CBootControl:
    MMCBLK_DEV_PREFIX = "mmcblk"  # internal emmc
    NVMESSD_DEV_PREFIX = "nvme"  # external nvme ssd
    INTERNAL_EMMC_DEVNAME = "mmcblk0"
    _slot_id_partid = {SlotID("0"): "1", SlotID("1"): "2"}

    def __init__(self):
        # ------ sanity check, confirm we are at jetson device ------ #
        if not os.path.exists(boot_cfg.TEGRA_CHIP_ID_PATH):
            _err_msg = (
                f"not a jetson device, {boot_cfg.TEGRA_CHIP_ID_PATH} doesn't exist"
            )
            logger.error(_err_msg)
            raise JetsonCBootContrlError(_err_msg)

        # ------ check BSP version ------ #
        try:
            self.bsp_version = bsp_version = parse_bsp_version(
                Path(boot_cfg.NV_TEGRA_RELEASE_FPATH).read_text()
            )
        except Exception as e:
            _err_msg = f"failed to detect BSP version: {e!r}"
            logger.error(_err_msg)
            raise JetsonCBootContrlError(_err_msg)
        logger.info(f"{bsp_version=}")

        # ------ sanity check, jetson-cboot is not used after BSP R34 ------ #
        if not bsp_version < (34, 0, 0):
            _err_msg = (
                f"jetson-cboot only supports BSP version < R34, but get {bsp_version=}. "
                "Please use jetson-uefi bootloader type for device with BSP >= R34."
            )
            logger.error(_err_msg)
            raise JetsonCBootContrlError(_err_msg)

        # ------ check if unified A/B is enabled ------ #
        # NOTE: mismatch rootfs BSP version and bootloader firmware BSP version
        #   is NOT supported and MUST not occur.
        unified_ab_enabled = False
        if bsp_version >= (32, 6, 0):
            # NOTE: unified A/B is supported starting from r32.6
            unified_ab_enabled = _NVBootctrl.is_unified_enabled()
            if unified_ab_enabled is None:
                _err_msg = "rootfs A/B is not enabled!"
                logger.error(_err_msg)
                raise JetsonCBootContrlError(_err_msg)
        else:  # R32.5 and below doesn't support unified A/B
            try:
                _NVBootctrl.get_current_slot(target="rootfs")
            except subprocess.CalledProcessError:
                _err_msg = "rootfs A/B is not enabled!"
                logger.error(_err_msg)
                raise JetsonCBootContrlError(_err_msg)
        self.unified_ab_enabled = unified_ab_enabled

        if unified_ab_enabled:
            logger.info(
                "unified A/B is enabled, rootfs and bootloader will be switched together"
            )

        # ------ check A/B slots ------ #
        self.current_bootloader_slot = current_bootloader_slot = (
            _NVBootctrl.get_current_slot()
        )
        self.standby_bootloader_slot = standby_bootloader_slot = (
            _NVBootctrl.get_standby_slot()
        )
        if not unified_ab_enabled:
            self.current_rootfs_slot = current_rootfs_slot = (
                _NVBootctrl.get_current_slot(target="rootfs")
            )
            self.standby_rootfs_slot = standby_rootfs_slot = (
                _NVBootctrl.get_standby_slot(target="rootfs")
            )
        else:
            self.current_rootfs_slot = current_rootfs_slot = current_bootloader_slot
            self.standby_rootfs_slot = standby_rootfs_slot = standby_bootloader_slot

        # check if rootfs slot and bootloader slot mismatches, this only happens
        #   when unified_ab is not enabled.
        if current_rootfs_slot != current_bootloader_slot:
            logger.warning(
                "bootloader and rootfs A/B slot mismatches: "
                f"{current_rootfs_slot=} != {current_bootloader_slot=}"
            )
            logger.warning("this might indicates a failed previous firmware update")

        # ------ detect rootfs_dev and parent_dev ------ #
        self.curent_rootfs_devpath = current_rootfs_devpath = (
            cmdhelper.get_current_rootfs_dev()
        )
        self.parent_devpath = parent_devpath = Path(
            cmdhelper.get_parent_dev(current_rootfs_devpath)
        )

        self._external_rootfs = False
        parent_devname = parent_devpath.name
        if parent_devname.startswith(boot_cfg.MMCBLK_DEV_PREFIX):
            logger.info(f"device boots from internal emmc: {parent_devpath}")
        elif parent_devname.startswith(boot_cfg.NVMESSD_DEV_PREFIX):
            logger.info(f"device boots from external nvme ssd: {parent_devpath}")
            self._external_rootfs = True
        else:
            _err_msg = f"we don't support boot from {parent_devpath=} currently"
            logger.error(_err_msg)
            raise JetsonCBootContrlError(_err_msg) from NotImplementedError(
                f"unsupported bootdev {parent_devpath}"
            )

        # rootfs partition
        self.standby_rootfs_devpath = (
            f"/dev/{parent_devname}p{self._slot_id_partid[standby_rootfs_slot]}"
        )
        self.standby_rootfs_dev_partuuid = cmdhelper.get_attrs_by_dev(
            "PARTUUID", f"{self.standby_rootfs_devpath}"
        )
        current_rootfs_dev_partuuid = cmdhelper.get_attrs_by_dev(
            "PARTUUID", current_rootfs_devpath
        )

        logger.info(
            "finish detecting rootfs devs: \n"
            f"active_slot({current_rootfs_slot}): {self.curent_rootfs_devpath=}, {current_rootfs_dev_partuuid=}\n"
            f"standby_slot({standby_rootfs_slot}): {self.standby_rootfs_devpath=}, {self.standby_rootfs_dev_partuuid=}"
        )

        # internal emmc partition
        self.standby_internal_emmc_devpath = f"/dev/{boot_cfg.INTERNAL_EMMC_DEVNAME}p{self._slot_id_partid[standby_rootfs_slot]}"

        logger.info(f"finished cboot control init: {current_rootfs_slot=}")
        logger.info(f"nvbootctrl dump-slots-info: \n{_NVBootctrl.dump_slots_info()}")
        if not unified_ab_enabled:
            logger.info(
                f"nvbootctrl -t rootfs dump-slots-info: \n{_NVBootctrl.dump_slots_info(target='rootfs')}"
            )

    # API

    @property
    def external_rootfs_enabled(self) -> bool:
        """Indicate whether rootfs on external storage is enabled.

        NOTE: distiguish from boot from external storage, as R32.5 and below doesn't
            support native NVMe boot.
        """
        return self._external_rootfs

    def set_standby_rootfs_unbootable(self):
        _NVBootctrl.set_slot_as_unbootable(self.standby_rootfs_slot, target="rootfs")

    def switch_boot_to_standby(self) -> None:
        # NOTE(20240412): we always try to align bootloader slot with rootfs.
        target_slot = self.standby_rootfs_slot

        logger.info(f"switch boot to standby slot({target_slot})")
        if not self.unified_ab_enabled:
            _NVBootctrl.set_active_boot_slot(target_slot, target="rootfs")

        # when unified_ab enabled, switching bootloader slot will also switch
        #   the rootfs slot.
        _NVBootctrl.set_active_boot_slot(target_slot)


class JetsonCBootControl(BootControllerProtocol):
    """BootControllerProtocol implementation for jetson-cboot."""

    def __init__(self) -> None:
        try:
            # startup boot controller
            self._cboot_control = _CBootControl()

            # mount point prepare
            self._mp_control = SlotMountHelper(
                standby_slot_dev=self._cboot_control.standby_rootfs_devpath,
                standby_slot_mount_point=cfg.MOUNT_POINT,
                active_slot_dev=self._cboot_control.curent_rootfs_devpath,
                active_slot_mount_point=cfg.ACTIVE_ROOT_MOUNT_POINT,
            )
            current_ota_status_dir = Path(boot_cfg.OTA_STATUS_DIR)
            standby_ota_status_dir = self._mp_control.standby_slot_mount_point / Path(
                boot_cfg.OTA_STATUS_DIR
            ).relative_to("/")

            # load firmware BSP version from current rootfs slot
            self._firmware_ver_control = FirmwareBSPVersionControl(
                current_firmware_bsp_vf=current_ota_status_dir
                / boot_cfg.FIRMWARE_BSP_VERSION_FNAME,
                standby_firmware_bsp_vf=standby_ota_status_dir
                / boot_cfg.FIRMWARE_BSP_VERSION_FNAME,
            )

            # init ota-status files
            self._ota_status_control = OTAStatusFilesControl(
                active_slot=str(self._cboot_control.current_rootfs_slot),
                standby_slot=str(self._cboot_control.standby_rootfs_slot),
                current_ota_status_dir=current_ota_status_dir,
                # NOTE: might not yet be populated before OTA update applied!
                standby_ota_status_dir=standby_ota_status_dir,
                finalize_switching_boot=self._finalize_switching_boot,
            )
        except Exception as e:
            _err_msg = f"failed to start jetson-cboot controller: {e!r}"
            raise ota_errors.BootControlStartupFailed(_err_msg, module=__name__) from e

    def _finalize_switching_boot(self) -> bool:
        """
        If firmware update failed(updated bootloader slot boot failed), clear the according slot's
            firmware_bsp_version information to force firmware update in next OTA.
        Also if unified A/B is NOT enabled and everything is alright, execute mark-boot-success <cur_slot>
            to mark the current booted rootfs boots successfully.
        """
        current_boot_slot = self._cboot_control.current_bootloader_slot
        current_rootfs_slot = self._cboot_control.current_rootfs_slot

        update_result = NVUpdateEngine.verify_update()
        if (retcode := update_result.returncode) != 0:
            _err_msg = (
                f"The previous firmware update failed(verify return {retcode}): \n"
                f"stderr: {update_result.stderr.decode()}\n"
                f"stdout: {update_result.stdout.decode()}\n"
                "failing the OTA and clear firmware version due to new bootloader slot boot failed."
            )
            logger.error(_err_msg)

            # NOTE: always only change current slots firmware_bsp_version file here.
            self._firmware_ver_control.set_version_by_slot(current_boot_slot, None)
            self._firmware_ver_control.write_current_firmware_bsp_version()
            return False

        # NOTE(20240417): rootfs slot is manually switched by set-active-boot-slot,
        #   so we need to manually set the slot as success after first reboot.
        if not self._cboot_control.unified_ab_enabled:
            _NVBootctrl.mark_boot_successful(current_rootfs_slot, target="rootfs")

        logger.info(
            f"nv_update_engine verify succeeded: \n{update_result.stdout.decode()}"
        )
        return True

    def _nv_firmware_update(self) -> Optional[bool]:
        """Perform firmware update with nv_update_engine.

        Returns:
            True if firmware update applied, False for failed firmware update,
                None for no firmware update occurs.
        """
        logger.info("jetson-cboot: entering nv firmware update ...")
        standby_bootloader_slot = self._cboot_control.standby_bootloader_slot
        standby_firmware_bsp_ver = self._firmware_ver_control.get_version_by_slot(
            standby_bootloader_slot
        )
        logger.info(f"{standby_bootloader_slot=} BSP ver: {standby_firmware_bsp_ver}")

        # ------ check if we need to do firmware update ------ #
        _new_bsp_v_fpath = self._mp_control.standby_slot_mount_point / Path(
            boot_cfg.NV_TEGRA_RELEASE_FPATH
        ).relative_to("/")
        try:
            new_bsp_v = parse_bsp_version(_new_bsp_v_fpath.read_text())
        except Exception as e:
            logger.warning(f"failed to detect new image's BSP version: {e!r}")
            logger.warning("skip firmware update due to new image BSP version unknown")
            return

        logger.info(f"BUP package version: {new_bsp_v=}")
        if standby_firmware_bsp_ver and standby_firmware_bsp_ver >= new_bsp_v:
            logger.info(
                f"{standby_bootloader_slot=} has newer or equal ver of firmware, skip firmware update"
            )
            return

        # ------ preform firmware update ------ #
        firmware_dpath = self._mp_control.standby_slot_mount_point / Path(
            boot_cfg.FIRMWARE_DPATH
        ).relative_to("/")

        _firmware_applied = False
        for firmware_fname in boot_cfg.FIRMWARE_LIST:
            if (firmware_fpath := firmware_dpath / firmware_fname).is_file():
                logger.info(f"nv_firmware: apply {firmware_fpath} ...")
                try:
                    NVUpdateEngine.apply_firmware_update(
                        firmware_fpath,
                        unified_ab=bool(self._cboot_control.unified_ab_enabled),
                    )
                    _firmware_applied = True
                except subprocess.CalledProcessError as e:
                    _err_msg = (
                        f"failed to apply BUP {firmware_fpath}: {e!r}, \n"
                        f"stderr={e.stderr.decode()}, \n"
                        f"stdout={e.stdout.decode()}"
                    )
                    logger.error(_err_msg)
                    logger.warning("firmware update interrupted, failing OTA...")

                    # if the firmware update is interrupted halfway(some of the BUP is applied),
                    #   revert bootloader slot switch
                    if _firmware_applied:
                        logger.warning(
                            "revert bootloader slot switch to current active slot"
                        )
                        _NVBootctrl.set_active_boot_slot(
                            self._cboot_control.current_bootloader_slot
                        )
                    return False

        # ------ register new firmware version ------ #
        if _firmware_applied:
            logger.info(
                f"nv_firmware: successfully apply firmware to {self._cboot_control.standby_rootfs_slot=}"
            )
            self._firmware_ver_control.set_version_by_slot(
                standby_bootloader_slot, new_bsp_v
            )
            return True

    # APIs

    def get_standby_slot_path(self) -> Path:
        return self._mp_control.standby_slot_mount_point

    def get_standby_boot_dir(self) -> Path:
        return self._mp_control.standby_boot_dir

    def pre_update(self, version: str, *, standby_as_ref: bool, erase_standby: bool):
        try:
            logger.info("jetson-cboot: pre-update ...")
            # udpate active slot's ota_status
            self._ota_status_control.pre_update_current()

            if not self._cboot_control.unified_ab_enabled:
                # set standby rootfs as unbootable as we are going to update it
                # this operation not applicable when unified A/B is enabled.
                self._cboot_control.set_standby_rootfs_unbootable()

            # prepare standby slot dev
            self._mp_control.prepare_standby_dev(erase_standby=erase_standby)
            # mount slots
            self._mp_control.mount_standby()
            self._mp_control.mount_active()

            # update standby slot's ota_status files
            self._ota_status_control.pre_update_standby(version=version)
        except Exception as e:
            _err_msg = f"failed on pre_update: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPreUpdateFailed(
                _err_msg, module=__name__
            ) from e

    def post_update(self) -> Generator[None, None, None]:
        try:
            logger.info("jetson-cboot: post-update ...")
            # ------ update extlinux.conf ------ #
            update_standby_slot_extlinux_cfg(
                active_slot_extlinux_fpath=Path(boot_cfg.EXTLINUX_FILE),
                standby_slot_extlinux_fpath=self._mp_control.standby_slot_mount_point
                / Path(boot_cfg.EXTLINUX_FILE).relative_to("/"),
                standby_slot_partuuid=self._cboot_control.standby_rootfs_dev_partuuid,
            )

            # ------ firmware update ------ #
            firmware_update_result = self._nv_firmware_update()
            if firmware_update_result:
                self._firmware_ver_control.write_current_firmware_bsp_version()
            elif firmware_update_result is None:
                logger.info("no firmware update occurs")
            else:
                raise JetsonCBootContrlError("firmware update failed")

            # ------ preserve BSP version files to standby slot ------ #
            self._firmware_ver_control.write_standby_firmware_bsp_version()

            # ------ preserve /boot/ota folder to standby rootfs ------ #
            preserve_ota_config_files_to_standby(
                active_slot_ota_dirpath=self._mp_control.active_slot_mount_point
                / "boot"
                / "ota",
                standby_slot_ota_dirpath=self._mp_control.standby_slot_mount_point
                / "boot"
                / "ota",
            )

            # ------ for external rootfs, preserve /boot folder to internal ------ #
            # NOTE: the copy must happen AFTER all the changes to active slot's /boot done.
            if self._cboot_control._external_rootfs:
                logger.info(
                    "rootfs on external storage enabled: "
                    "copy standby slot rootfs' /boot folder "
                    "to corresponding internal emmc dev ..."
                )
                copy_standby_slot_boot_to_internal_emmc(
                    internal_emmc_mp=Path(boot_cfg.SEPARATE_BOOT_MOUNT_POINT),
                    internal_emmc_devpath=Path(
                        self._cboot_control.standby_internal_emmc_devpath
                    ),
                    standby_slot_boot_dirpath=self._mp_control.standby_slot_mount_point
                    / "boot",
                )

            # ------ switch boot to standby ------ #
            self._cboot_control.switch_boot_to_standby()

            # ------ prepare to reboot ------ #
            self._mp_control.umount_all(ignore_error=True)
            logger.info(f"[post-update]: \n{_NVBootctrl.dump_slots_info()}")
            logger.info("post update finished, wait for reboot ...")
            yield  # hand over control back to otaclient
            cmdhelper.reboot()
        except Exception as e:
            _err_msg = f"failed on post_update: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPostUpdateFailed(
                _err_msg, module=__name__
            ) from e

    def pre_rollback(self):
        try:
            logger.info("jetson-cboot: pre-rollback setup ...")
            self._ota_status_control.pre_rollback_current()
            self._mp_control.mount_standby()
            self._ota_status_control.pre_rollback_standby()
        except Exception as e:
            _err_msg = f"failed on pre_rollback: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPreRollbackFailed(
                _err_msg, module=__name__
            ) from e

    def post_rollback(self):
        try:
            logger.info("jetson-cboot: post-rollback setup...")
            self._mp_control.umount_all(ignore_error=True)
            self._cboot_control.switch_boot_to_standby()
            cmdhelper.reboot()
        except Exception as e:
            _err_msg = f"failed on post_rollback: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPostRollbackFailed(
                _err_msg, module=__name__
            ) from e

    def on_operation_failure(self):
        """Failure registering and cleanup at failure."""
        logger.warning("on failure try to unmounting standby slot...")
        self._ota_status_control.on_failure()
        self._mp_control.umount_all(ignore_error=True)

    def load_version(self) -> str:
        return self._ota_status_control.load_active_slot_version()

    def get_booted_ota_status(self) -> api_types.StatusOta:
        return self._ota_status_control.booted_ota_status
