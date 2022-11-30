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


r"""Boot control support for Raspberry pi 4."""
import os
from pathlib import Path

from .. import log_setting
from ..errors import (
    BootControlInitError,
    BootControlPostRollbackFailed,
    BootControlPostUpdateFailed,
    BootControlPreRollbackFailed,
    BootControlPreUpdateFailed,
    OTAError,
)
from ..proto import wrapper
from ..common import replace_atomic, subprocess_call

from ._common import (
    OTAStatusFilesControl,
    SlotMountHelper,
    CMDHelperFuncs,
)
from .configs import rpi_boot_cfg as cfg
from .protocol import BootControllerProtocol

logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class _RPIBootControl:
    """Boot control helper for rpi4 support.

    Expected partition layout:
        /dev/sda:
            - sda1: fat32, fslabel=systemb-boot
            - sda2: ext4, fslabel=slot_a
            - sda3: ext4, fslabel=slot_b
    slot is the fslabel for each AB rootfs.

    This class provides the following features:
    1. AB partition detection,
    2. boot files checking,
    3. switch boot(tryboot.txt setup and tryboot reboot),
    4. finalize switching boot.

    Boot files for each slot have the following naming format:
        <boot_file_fname>_<slot>
    i.e., config.txt for slot_a will be config.txt_slot_a
    """

    SLOT_A = cfg.SLOT_A_FSLABEL
    SLOT_B = cfg.SLOT_B_FSLABEL
    AB_FLIPS = {
        SLOT_A: SLOT_B,
        SLOT_B: SLOT_A,
    }
    SEP_CHAR = "_"

    def __init__(self) -> None:
        self.system_boot_path = Path(cfg.SYSTEM_BOOT_MOUNT_POINT)
        if not (
            self.system_boot_path.is_dir()
            and CMDHelperFuncs.is_target_mounted(self.system_boot_path)
        ):
            _err_msg = "system-boot is not presented or not mounted!"
            logger.error(_err_msg)
            raise BootControlInitError(_err_msg)
        self._init_slots_info()
        self._init_boot_files()

    def _init_slots_info(self):
        """Get current/standby slots info."""
        try:
            self._active_slot_dev = CMDHelperFuncs.get_dev_by_mount_point(
                cfg.ACTIVE_ROOTFS_PATH
            )
            self._active_slot = CMDHelperFuncs.get_fslabel_by_dev(self._active_slot_dev)
            self._standby_slot = self.AB_FLIPS[self._active_slot]
            self._standby_slot_dev = CMDHelperFuncs.get_dev_by_fslabel(
                self._standby_slot
            )
        except Exception as e:
            _err_msg = "failed to detect AB partition"
            logger.error(_err_msg)
            raise BootControlInitError(_err_msg) from e

    def _init_boot_files(self):
        """Check the availability of boot files.

        The following boot files will be checked:
        1. config.txt_<slot_suffix> (required)
        2. cmdline.txt_<slot_suffix> (required)
        3. vmlinuz_<slot_suffix>
        4. initrd.img_<slot_suffix>

        If any of the required files are missing, BootControlInitError will be raised.
        In such case, a reinstall/setup of AB partition boot files is required.
        """
        # boot file
        self.config_txt = self.system_boot_path / cfg.CONFIG_TXT
        self.tryboot_txt = self.system_boot_path / cfg.TRYBOOT_TXT

        # active slot
        self.config_txt_active_slot = (
            self.system_boot_path / f"{cfg.CONFIG_TXT}{self.SEP_CHAR}{self.active_slot}"
        )
        if not self.config_txt_active_slot.is_file():
            _err_msg = f"missing {self.config_txt_active_slot=}"
            logger.error(_err_msg)
            raise BootControlInitError(_err_msg)
        self.cmdline_txt_active_slot = (
            self.system_boot_path
            / f"{cfg.CMDLINE_TXT}{self.SEP_CHAR}{self.active_slot}"
        )
        if not self.cmdline_txt_active_slot.is_file():
            _err_msg = f"missing {self.cmdline_txt_active_slot=}"
            logger.error(_err_msg)
            raise BootControlInitError(_err_msg)
        self.vmlinuz_active_slot = (
            self.system_boot_path / f"{cfg.VMLINUZ}{self.SEP_CHAR}{self.active_slot}"
        )
        self.initrd_img_active_slot = (
            self.system_boot_path / f"{cfg.INITRD_IMG}{self.SEP_CHAR}{self.active_slot}"
        )
        # standby slot
        self.config_txt_standby_slot = (
            self.system_boot_path
            / f"{cfg.CONFIG_TXT}{self.SEP_CHAR}{self.standby_slot}"
        )
        if not self.config_txt_standby_slot.is_file():
            _err_msg = f"missing {self.config_txt_standby_slot=}"
            logger.error(_err_msg)
            raise BootControlInitError(_err_msg)
        self.cmdline_txt_standby_slot = (
            self.system_boot_path
            / f"{cfg.CMDLINE_TXT}{self.SEP_CHAR}{self.standby_slot}"
        )
        if not self.cmdline_txt_standby_slot.is_file():
            _err_msg = f"missing {self.cmdline_txt_standby_slot=}"
            logger.error(_err_msg)
            raise BootControlInitError(_err_msg)
        self.vmlinuz_standby_slot = (
            self.system_boot_path / f"{cfg.VMLINUZ}{self.SEP_CHAR}{self.standby_slot}"
        )
        self.initrd_img_standby_slot = (
            self.system_boot_path
            / f"{cfg.INITRD_IMG}{self.SEP_CHAR}{self.standby_slot}"
        )

    def _update_firmware(self):
        """Call external flash_kernel system script to install new firmware.

        NOTE: this script also flashes kernel and initrd.img to /boot/firmware besides
        dtb files, so we should also handle that.
        """
        try:
            # call flash-kernel to install new dtb files, boot firmwares and kernel, initrd.img
            # from current rootfs to system-boot partition
            logger.info("update firmware with flash-kernel...")
            _cmd = "flash-kernel"
            subprocess_call(_cmd, raise_exception=True)
            os.sync()

            # check if the vmlinuz and initrd.img presented in /boot/firmware(system-boot),
            # if so, it means that flash-kernel works and copies the kernel, inird.img from /boot,
            # then we rename vmlinuz and initrd.img to vmlinuz_<current_slot> and initrd.img_<current_slot>
            _vmlinuz = Path(cfg.SYSTEM_BOOT_MOUNT_POINT) / cfg.VMLINUZ
            _initrd_img = Path(cfg.SYSTEM_BOOT_MOUNT_POINT) / cfg.INITRD_IMG
            if _vmlinuz.is_file():
                os.replace(_vmlinuz, self.vmlinuz_active_slot)
            if _initrd_img.is_file():
                os.replace(_initrd_img, self.initrd_img_active_slot)
            logger.info("firmware updated")
        except Exception as e:
            logger.error(f"_init_flash_kernel failed: {e!r}")
            raise

    # exposed API methods/properties
    @property
    def active_slot(self) -> str:
        return self._active_slot

    @property
    def standby_slot(self) -> str:
        return self._standby_slot

    @property
    def standby_slot_dev(self) -> str:
        return self._standby_slot_dev

    @property
    def active_slot_dev(self) -> str:
        return self._active_slot_dev

    def finalize_switching_boot(self) -> bool:
        """Finalize switching boot by swapping config.txt and tryboot.txt if we should.

        Swiching boot mechanism:
            1. atomically replace tryboot.txt with tryboot.txt_standby_slot
            2. atomically replace config.txt with config.txt_active_slot
        Also finalize_switching_boot will try to update the firmware by calling external
        flash-kernel system script.

        Returns:
            A bool indicates whether the switch boot succeeded or not. Note that no exception
                will be raised if finalizing failed.
        """
        try:
            self._update_firmware()
            replace_atomic(self.config_txt_active_slot, self.config_txt)
            replace_atomic(self.config_txt_standby_slot, self.tryboot_txt)
            return True
        except Exception as e:
            _err_msg = f"failed to finalize boot switching: {e!r}"
            logger.error(_err_msg)
            return False

    def prepare_tryboot_txt(self):
        """Copy the standby slot's config.txt as tryboot.txt."""
        try:
            replace_atomic(self.config_txt_standby_slot, self.tryboot_txt)
        except Exception as e:
            _err_msg = f"failed to prepare tryboot.txt for {self._standby_slot}"
            logger.error(_err_msg)
            raise BootControlPostUpdateFailed(_err_msg) from e

    def reboot_tryboot(self):
        """Reboot with tryboot flag."""
        try:
            _cmd = "reboot '0 tryboot'"
            subprocess_call(_cmd, raise_exception=True)
        except Exception as e:
            _err_msg = "failed to reboot"
            logger.exception(_err_msg)
            raise BootControlPostUpdateFailed(_err_msg) from e


class RPIBootController(BootControllerProtocol):
    """BootControllerProtocol implementation for rpi4 support."""

    def __init__(self) -> None:
        self._rpiboot_control = _RPIBootControl()

        ### mount point prepare ###
        self._mp_control = SlotMountHelper(
            standby_slot_dev=self._rpiboot_control.standby_slot_dev,
            standby_slot_mount_point=cfg.MOUNT_POINT,
            active_slot_dev=self._rpiboot_control.active_slot_dev,
            active_slot_mount_point=cfg.REF_ROOT_MOUNT_POINT,
        )

        ### init ota-status files ###
        self._ota_status_control = OTAStatusFilesControl(
            active_slot=self._rpiboot_control.active_slot,
            standby_slot=self._rpiboot_control.standby_slot,
            current_ota_status_dir=Path(cfg.ACTIVE_ROOTFS_PATH)
            / Path(cfg.OTA_STATUS_DIR).relative_to("/"),
            # NOTE: might not yet be populated before OTA update applied!
            standby_ota_status_dir=Path(cfg.MOUNT_POINT)
            / Path(cfg.OTA_STATUS_DIR).relative_to("/"),
            finalize_switching_boot=self._rpiboot_control.finalize_switching_boot,
        )

    def _copy_kernel_for_standby_slot(self):
        """Copy the kernel and initrd_img files from current slot /boot
        to system-boot for standby slot.

        This method checkouts the vmlinuz and initrd.img symlinks under /boot,
        and copy them to /boot/firmware(system-boot partition) under the name
        vmlinuz_<slot> and initrd.img_<slot>.
        """
        try:
            _kernel, _initrd_img = (
                self._mp_control.standby_boot_dir / cfg.VMLINUZ,
                self._mp_control.standby_boot_dir / cfg.INITRD_IMG,
            )
            _kernel_sysboot, _initrd_img_sysboot = (
                self._rpiboot_control.vmlinuz_standby_slot,
                self._rpiboot_control.initrd_img_standby_slot,
            )
            replace_atomic(_kernel, _kernel_sysboot)
            replace_atomic(_initrd_img, _initrd_img_sysboot)
        except Exception as e:
            _err_msg = "failed to copy kernel/initrd_img for standby slot"
            logger.error(_err_msg)
            raise BootControlPostUpdateFailed(_err_msg) from e

    # APIs

    def get_standby_slot_path(self) -> Path:
        return self._mp_control.standby_slot_mount_point

    def get_standby_boot_dir(self) -> Path:
        return self._mp_control.standby_boot_dir

    def pre_update(self, version: str, *, standby_as_ref: bool, erase_standby: bool):
        try:
            logger.info("rpi_boot: pre-update setup...")
            ### udpate active slot's ota_status ###
            self._ota_status_control.pre_update_current()

            ### mount slots ###
            self._mp_control.mount_standby(erase_standby=erase_standby)
            self._mp_control.mount_active()

            ### update standby slot's ota_status files ###
            self._ota_status_control.pre_update_standby(version=version)
        except Exception as e:
            _err_msg = f"failed on pre_update: {e!r}"
            logger.error(_err_msg)
            raise BootControlPreUpdateFailed(_err_msg) from e

    def pre_rollback(self):
        try:
            logger.info("rpi_boot: pre-rollback setup...")
            self._ota_status_control.pre_rollback_current()
            self._mp_control.mount_standby(erase_standby=False)
            self._ota_status_control.pre_rollback_standby()
        except Exception as e:
            _err_msg = f"failed on pre_rollback: {e!r}"
            logger.error(_err_msg)
            raise BootControlPreRollbackFailed(_err_msg) from e

    def post_rollback(self):
        try:
            logger.info("rpi_boot: post-rollback setup...")
            self._rpiboot_control.prepare_tryboot_txt()
            self._mp_control.umount_all(ignore_error=True)
            self._rpiboot_control.reboot_tryboot()
        except OTAError:
            raise
        except Exception as e:
            raise BootControlPostRollbackFailed from e

    def post_update(self):
        try:
            logger.info("rpi_boot: post-update setup...")
            self._copy_kernel_for_standby_slot()
            self._rpiboot_control.prepare_tryboot_txt()
            self._mp_control.umount_all(ignore_error=True)
            self._rpiboot_control.reboot_tryboot()
        except OTAError:
            raise
        except Exception as e:
            raise BootControlPostUpdateFailed from e

    def on_operation_failure(self):
        """Failure registering and cleanup at failure."""
        logger.warning("on failure try to unmounting standby slot...")
        self._ota_status_control.on_failure()
        self._mp_control.umount_all(ignore_error=True)

    def load_version(self) -> str:
        return self._ota_status_control.load_active_slot_version()

    def get_ota_status(self) -> wrapper.StatusOta:
        return self._ota_status_control.ota_status
