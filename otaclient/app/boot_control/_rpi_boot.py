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

from . import _errors
from ._common import (
    OTAStatusFilesControlMixin,
    PrepareMountMixin,
    CMDHelperFuncs,
)
from .configs import rpi_boot_cfg as cfg
from .protocol import BootControllerProtocol

logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class _RPIBootControl:
    """Actually logics for rpi4 support.

    Expected partition layout:
        /dev/sda:
            - sda1: fslabel: systemb-boot
            - sda2: fslabel: slot_a
            - sda3: fslabel: slot_b

    slot is the fslabel for each AB rootfs.
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
        """Get current/standby slot info."""
        try:
            self.active_slot_dev = CMDHelperFuncs.get_dev_by_mount_point(
                cfg.ACTIVE_ROOTFS_PATH
            )
            self.active_slot = CMDHelperFuncs.get_fslabel_by_dev(self.active_slot_dev)
            self.standby_slot = self.AB_FLIPS[self.active_slot]
            self.standby_slot_dev = CMDHelperFuncs.get_dev_by_fslabel(self.standby_slot)
        except Exception as e:
            _err_msg = "failed to detect AB partition"
            logger.error(_err_msg)
            raise BootControlInitError(_err_msg) from e

    def _init_boot_files(self):
        """Check the availability of boot files.

        Expecting config.txt_<slot_suffix>, cmdline.txt_<slot_suffix>,
        vmlinuz_<slot_suffix> and initrd.img_<slot_suffix> exists.

        If any of config.txt or cmdline.txt is missing, raise BootControlInitError.
        In such case, a reinstall/setup of A/B partition boot files is required.

        TODO: bootstrap boot files if any is missing.
        """
        # boot file
        self.config_txt = self.system_boot_path / cfg.CONFIG_TXT
        self.tryboot_txt = self.system_boot_path / cfg.TRYBOOT_TXT

        # active slot
        self.config_txt_active_slot = (
            Path(cfg.SYSTEM_BOOT_MOUNT_POINT)
            / f"{cfg.CONFIG_TXT}{self.SEP_CHAR}{self.active_slot}"
        )
        if not self.config_txt_active_slot.is_file():
            _err_msg = f"missing {self.config_txt_active_slot=}"
            logger.error(_err_msg)
            raise BootControlInitError(_err_msg)
        self.cmdline_txt_active_slot = (
            Path(cfg.SYSTEM_BOOT_MOUNT_POINT)
            / f"{cfg.CMDLINE_TXT}{self.SEP_CHAR}{self.active_slot}"
        )
        if not self.cmdline_txt_active_slot.is_file():
            _err_msg = f"missing {self.cmdline_txt_active_slot=}"
            logger.error(_err_msg)
            raise BootControlInitError(_err_msg)
        self.vmlinuz_active_slot = (
            Path(cfg.SYSTEM_BOOT_MOUNT_POINT)
            / f"{cfg.VMLINUZ}{self.SEP_CHAR}{self.active_slot}"
        )
        self.initrd_img_active_slot = (
            Path(cfg.SYSTEM_BOOT_MOUNT_POINT)
            / f"{cfg.INITRD_IMG}{self.SEP_CHAR}{self.active_slot}"
        )
        # standby slot
        self.config_txt_standby_slot = (
            Path(cfg.SYSTEM_BOOT_MOUNT_POINT)
            / f"{cfg.CONFIG_TXT}{self.SEP_CHAR}{self.standby_slot}"
        )
        if not self.config_txt_standby_slot.is_file():
            _err_msg = f"missing {self.config_txt_standby_slot=}"
            logger.error(_err_msg)
            raise BootControlInitError(_err_msg)
        self.cmdline_txt_standby_slot = (
            Path(cfg.SYSTEM_BOOT_MOUNT_POINT)
            / f"{cfg.CMDLINE_TXT}{self.SEP_CHAR}{self.standby_slot}"
        )
        if not self.cmdline_txt_standby_slot.is_file():
            _err_msg = f"missing {self.cmdline_txt_standby_slot=}"
            logger.error(_err_msg)
            raise BootControlInitError(_err_msg)
        self.vmlinuz_standby_slot = (
            Path(cfg.SYSTEM_BOOT_MOUNT_POINT)
            / f"{cfg.VMLINUZ}{self.SEP_CHAR}{self.standby_slot}"
        )
        self.initrd_img_standby_slot = (
            Path(cfg.SYSTEM_BOOT_MOUNT_POINT)
            / f"{cfg.INITRD_IMG}{self.SEP_CHAR}{self.standby_slot}"
        )

    def finalize_switching_boot(self):
        """Finalize switching boot by swapping config.txt and tryboot.txt if we should.

        Swiching boot mechanism:
            1. atomically replace tryboot.txt with tryboot.txt_standby_slot
            2. atomically replace config.txt with config.txt_active_slot
        """
        try:
            replace_atomic(self.config_txt_active_slot, self.config_txt)
            replace_atomic(self.config_txt_standby_slot, self.tryboot_txt)
        except Exception as e:
            _err_msg = "failed to finalize boot switching"
            logger.error(_err_msg)
            raise BootControlInitError(_err_msg) from e

    def prepare_tryboot_txt(self):
        """Copy the standby slot's config.txt as tryboot.txt."""
        try:
            replace_atomic(self.config_txt_standby_slot, self.tryboot_txt)
        except Exception as e:
            _err_msg = f"failed to prepare tryboot.txt for {self.standby_slot}"
            logger.error(_err_msg)
            raise BootControlPostUpdateFailed(_err_msg) from e

    def reboot_tryboot(self):
        """Reboot with tryboot flag."""
        try:
            _cmd = "reboot 0 tryboot"
            subprocess_call(_cmd, raise_exception=True)
        except Exception as e:
            _err_msg = "failed to reboot"
            logger.exception(_err_msg)
            raise BootControlPostUpdateFailed(_err_msg) from e


class RPIBootController(
    PrepareMountMixin,
    OTAStatusFilesControlMixin,
    BootControllerProtocol,
):
    """RPIBootController implements BootControllerProtocol for rpi4 support."""

    def __init__(self) -> None:
        self._rpiboot_control = _RPIBootControl()
        # slots
        self.standby_slot = self._rpiboot_control.standby_slot
        self.active_slot = self._rpiboot_control.active_slot
        # slots_dev
        self.standby_slot_dev = self._rpiboot_control.standby_slot_dev
        self.active_slot_dev = self._rpiboot_control.active_slot_dev

        ### mount point prepare ###
        self.standby_slot_mount_point = Path(cfg.MOUNT_POINT)
        self.standby_boot_dir = self.standby_slot_mount_point / "boot"
        self.ref_slot_mount_point = Path(cfg.REF_ROOT_MOUNT_POINT)
        self._prepare_mount_points()

        ### init ota-status files ###
        # ota-status folder
        self.current_ota_status_dir = Path(cfg.ACTIVE_ROOTFS_PATH) / Path(
            cfg.OTA_STATUS_DIR
        ).relative_to(cfg.ACTIVE_ROOTFS_PATH)
        # NOTE: might not yet be populated before OTA update applied!
        self.standby_ota_status_dir = self.standby_slot_mount_point / Path(
            cfg.OTA_STATUS_DIR
        ).relative_to(cfg.ACTIVE_ROOTFS_PATH)
        # init ota_status files for current slot
        self._init_ota_status_files(
            active_slot=self.active_slot,
            finalize_switching_boot=self._rpiboot_control.finalize_switching_boot,
        )

    def _copy_standby_kernel_and_initrd_img(self):
        """Copy the kernel and initrd_img file to system-boot for standby slot."""
        try:
            _kernel, _initrd_img = (
                self.standby_boot_dir / cfg.VMLINUZ,
                self.standby_boot_dir / cfg.INITRD_IMG,
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
        return self.standby_slot_mount_point

    def get_standby_boot_dir(self) -> Path:
        return self.standby_boot_dir

    def pre_update(self, version: str, *, standby_as_ref: bool, erase_standby: bool):
        try:
            ### udpate active slot's ota_status ###
            self._pre_update_set_current_ota_status_files(
                standby_slot=self.standby_slot
            )

            ### mount slots ###
            self._prepare_and_mount_standby(
                self.standby_slot_dev,
                erase=erase_standby,
            )
            self._mount_refroot(
                standby_dev=self.standby_slot_dev,
                active_dev=self.active_slot_dev,
                standby_as_ref=standby_as_ref,
            )

            ### update standby slot's ota_status files ###
            self._pre_update_set_standby_ota_status_files(
                standby_slot=self.standby_slot, version=version
            )

            logger.info("pre-update setting finished")
        except _errors.BootControlError as e:
            _err_msg = f"failed on pre_update: {e!r}"
            logger.error(_err_msg)
            raise BootControlPreUpdateFailed(_err_msg) from e

    def pre_rollback(self):
        try:
            self._pre_rollback_set_current_ota_status_files()
            self._prepare_and_mount_standby(
                self.standby_slot_dev,
                erase=False,
            )
            self._pre_rollback_set_standby_ota_status_files()
        except Exception as e:
            _err_msg = f"failed on pre_rollback: {e!r}"
            logger.error(_err_msg)
            raise BootControlPreRollbackFailed(_err_msg) from e

    def post_rollback(self):
        try:
            self._rpiboot_control.prepare_tryboot_txt()
            self._rpiboot_control.reboot_tryboot()
        except OTAError:
            raise
        except Exception as e:
            raise BootControlPostRollbackFailed from e

    def post_update(self):
        try:
            self._copy_standby_kernel_and_initrd_img()
            self._rpiboot_control.prepare_tryboot_txt()
            self._rpiboot_control.reboot_tryboot()
        except OTAError:
            raise
        except Exception as e:
            raise BootControlPostUpdateFailed from e

    def on_operation_failure(self):
        """Failure registering and cleanup at failure."""
        self._store_current_status(wrapper.StatusOta.FAILURE)
        # when standby slot is not created, otastatus is not needed to be set
        if CMDHelperFuncs.is_target_mounted(self.standby_slot_mount_point):
            self._store_standby_status(wrapper.StatusOta.FAILURE)

        logger.warning("on failure try to unmounting standby slot...")
        self._umount_all(ignore_error=True)
