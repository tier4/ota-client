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


r"""Boot control support for Raspberry pi 4 Model B."""
import os
import re
from string import Template
from pathlib import Path
from typing import Generator

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
    write_str_to_file_sync,
)
from .configs import rpi_boot_cfg as cfg
from .protocol import BootControllerProtocol

logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)

_FSTAB_TEMPLATE_STR = (
    "LABEL=${rootfs_fslabel}\t/\text4\tdiscard,x-systemd.growfs\t0\t1\n"
    "LABEL=system-boot\t/boot/firmware\tvfat\tdefaults\t0\t1\n"
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
        logger.debug("checking and initializing slots info...")
        try:
            # detect active slot
            self._active_slot_dev = CMDHelperFuncs.get_dev_by_mount_point(
                cfg.ACTIVE_ROOTFS_PATH
            )
            if not (
                _active_slot := CMDHelperFuncs.get_fslabel_by_dev(self._active_slot_dev)
            ):
                raise ValueError(
                    f"failed to get slot_id(fslabel) for active slot dev({self._active_slot_dev})"
                )
            self._active_slot = _active_slot

            # detect standby slot
            # NOTE: using the similar logic like grub, detect the silibing dev
            #       of the active slot as standby slot
            _parent = CMDHelperFuncs.get_parent_dev(self._active_slot_dev)
            # list children device file from parent device
            # exclude parent dev(always in the front)
            # expected raw result from lsblk:
            # NAME="/dev/sdx"
            # NAME="/dev/sdx1" # system-boot
            # NAME="/dev/sdx2" # slot_a
            # NAME="/dev/sdx3" # slot_b
            _raw_child_partitions = CMDHelperFuncs._lsblk(f"-Pp -o NAME {_parent}")
            try:
                # NOTE: exclude the first 2 lines(parent and system-boot)
                _child_partitions = list(
                    map(
                        lambda _raw: _raw.split("=")[-1].strip('"'),
                        _raw_child_partitions.splitlines()[2:],
                    )
                )
                if (
                    len(_child_partitions) != 2
                    or self._active_slot_dev not in _child_partitions
                ):
                    raise ValueError
                _child_partitions.remove(self._active_slot_dev)
            except Exception:
                raise ValueError(
                    f"unexpected partition layout: {_raw_child_partitions}"
                )
            # it is OK if standby_slot dev doesn't have fslabel or fslabel != standby_slot_id
            # we will always set the fslabel
            self._standby_slot = self.AB_FLIPS[self._active_slot]
            self._standby_slot_dev = _child_partitions[0]
            logger.info(
                f"rpi_boot: active_slot: {self._active_slot}({self._active_slot_dev}), "
                f"standby_slot: {self._standby_slot}({self._standby_slot_dev})"
            )
        except Exception as e:
            _err_msg = f"failed to detect AB partition: {e!r}"
            logger.error(_err_msg)
            raise BootControlInitError(_err_msg) from None

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
        logger.debug("checking boot files...")
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
        """Call flash-kernel to install new dtb files, boot firmwares and kernel, initrd.img
        from current rootfs to system-boot partition.
        """
        logger.info("update firmware with flash-kernel...")
        try:
            subprocess_call("flash-kernel", raise_exception=True)
            os.sync()
        except Exception as e:
            logger.error(f"flash-kernel failed: {e!r}")
            raise

        try:
            # check if the vmlinuz and initrd.img presented in /boot/firmware(system-boot),
            # if so, it means that flash-kernel works and copies the kernel, inird.img from /boot,
            # then we rename vmlinuz and initrd.img to vmlinuz_<current_slot> and initrd.img_<current_slot>
            if (_vmlinuz := Path(cfg.SYSTEM_BOOT_MOUNT_POINT) / cfg.VMLINUZ).is_file():
                os.replace(_vmlinuz, self.vmlinuz_active_slot)
            if (
                _initrd_img := Path(cfg.SYSTEM_BOOT_MOUNT_POINT) / cfg.INITRD_IMG
            ).is_file():
                os.replace(_initrd_img, self.initrd_img_active_slot)
            os.sync()
        except Exception:
            logger.error(f"apply new kernel,initrd.img for {self.active_slot} failed")
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

        Finalize switch boot:
            1. atomically replace tryboot.txt with tryboot.txt_standby_slot
            2. atomically replace config.txt with config.txt_active_slot

        Two-stage reboot:
            In the first reboot after ota update applied, finalize_switching_boot will try
            to update the firmware by calling external flash-kernel system script, finalize
            the switch boot and then reboot again to apply the new firmware.
            In the second reboot, finalize_switching_boot will do nothing but just return True.

        NOTE: we use a SWITCH_BOOT_FLAG_FILE to distinguish which reboot we are right now.
              If SWITCH_BOOT_FLAG_FILE presented, we know that we are second reboot,
              Otherwise, we know that we are at first reboot after switching boot.

        Returns:
            A bool indicates whether the switch boot succeeded or not. Note that no exception
                will be raised if finalizing failed.
        """
        logger.info("finalizing switch boot...")
        try:
            _flag_file = self.system_boot_path / cfg.SWITCH_BOOT_FLAG_FILE
            # we are at second reboot, after firmware_update,
            # finalize the switch boot and then return True
            if _flag_file.is_file():
                # cleanup bak files generated by flash-kernel script as
                # we actually don't use those files
                for _bak_file in self.system_boot_path.glob("**/*.bak"):
                    _bak_file.unlink(missing_ok=True)
                _flag_file.unlink(missing_ok=True)
                os.sync()
                return True

            # we are at first reboot after ota update applied,
            # finalize the switch boot, install the new firmware and then reboot again
            else:
                replace_atomic(self.config_txt_active_slot, self.config_txt)
                replace_atomic(self.config_txt_standby_slot, self.tryboot_txt)
                logger.info(
                    "finalizing boot configuration,"
                    f"replace {self.config_txt=} with {self.config_txt_active_slot=}, "
                    f"replace {self.tryboot_txt=} with {self.config_txt_standby_slot=}"
                )
                self._update_firmware()
                # set the flag file
                write_str_to_file_sync(_flag_file, "")
                # reboot to the same slot to apply the new firmware

                logger.info("reboot to apply new firmware...")
                CMDHelperFuncs.reboot()
                return True
        except Exception as e:
            _err_msg = f"failed to finalize boot switching: {e!r}"
            logger.error(_err_msg)
            return False

    def prepare_standby_dev(self, *, erase_standby: bool):
        # try umount and dev
        if CMDHelperFuncs.is_target_mounted(self.standby_slot_dev):
            CMDHelperFuncs.umount(self.standby_slot_dev)
        try:
            if erase_standby:
                CMDHelperFuncs.mkfs_ext4(
                    self.standby_slot_dev,
                    fslabel=self.standby_slot,
                )
            else:
                # TODO: check the standby file system status
                #       if not erase the standby slot
                # set the standby file system label with standby slot id
                CMDHelperFuncs.set_dev_fslabel(self.active_slot_dev, self.standby_slot)
        except Exception as e:
            _err_msg = f"failed to prepare standby dev: {e!r}"
            raise BootControlPreUpdateFailed(_err_msg) from e

    def prepare_tryboot_txt(self):
        """Copy the standby slot's config.txt as tryboot.txt."""
        logger.debug("prepare tryboot.txt...")
        try:
            replace_atomic(self.config_txt_standby_slot, self.tryboot_txt)
            logger.info(
                f"replace {self.tryboot_txt=} with {self.config_txt_standby_slot=}"
            )
        except Exception as e:
            _err_msg = f"failed to prepare tryboot.txt for {self._standby_slot}"
            logger.error(_err_msg)
            raise BootControlPostUpdateFailed(_err_msg) from e

    def reboot_tryboot(self):
        """Reboot with tryboot flag."""
        logger.info(f"tryboot reboot to standby slot({self.standby_slot})...")
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
        # mount point prepare
        self._mp_control = SlotMountHelper(
            standby_slot_dev=self._rpiboot_control.standby_slot_dev,
            standby_slot_mount_point=cfg.MOUNT_POINT,
            active_slot_dev=self._rpiboot_control.active_slot_dev,
            active_slot_mount_point=cfg.ACTIVE_ROOT_MOUNT_POINT,
        )
        # init ota-status files
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
        logger.debug("rpi_boot initialization finished")

    def _copy_kernel_for_standby_slot(self):
        """Copy the kernel and initrd_img files from current slot /boot
        to system-boot for standby slot.

        This method will checkout the vmlinuz-<kernel_ver> and initrd.img-<kernel_ver>
        under /boot, and copy them to /boot/firmware(system-boot partition) under the name
        vmlinuz_<slot> and initrd.img_<slot>.
        """
        logger.debug(
            "prepare standby slot's kernel/initrd.img to system-boot partition..."
        )
        try:
            # search for kernel
            _kernel_pa, _kernel_ver = (
                re.compile(rf"{cfg.VMLINUZ}-(?P<kernel_ver>.*)"),
                None,
            )
            # NOTE: if there is multiple kernel, pick the first one we encounted
            # NOTE 2: according to ota-image specification, it should only be one
            #         version of kernel and initrd.img
            for _candidate in self._mp_control.standby_boot_dir.glob(
                f"{cfg.VMLINUZ}-*"
            ):
                if _ma := _kernel_pa.match(_candidate.name):
                    _kernel_ver = _ma.group("kernel_ver")
                    break

            if _kernel_ver is not None:
                _kernel, _initrd_img = (
                    self._mp_control.standby_boot_dir / f"{cfg.VMLINUZ}-{_kernel_ver}",
                    self._mp_control.standby_boot_dir
                    / f"{cfg.INITRD_IMG}-{_kernel_ver}",
                )
                _kernel_sysboot, _initrd_img_sysboot = (
                    self._rpiboot_control.vmlinuz_standby_slot,
                    self._rpiboot_control.initrd_img_standby_slot,
                )
                replace_atomic(_kernel, _kernel_sysboot)
                replace_atomic(_initrd_img, _initrd_img_sysboot)
            else:
                raise ValueError("failed to kernel in /boot folder at standby slot")
        except Exception as e:
            _err_msg = "failed to copy kernel/initrd_img for standby slot"
            logger.error(_err_msg)
            raise BootControlPostUpdateFailed(f"{e!r}")

    def _write_standby_fstab(self):
        """Override the standby's fstab file.

        The fstab file will contain 2 lines, one line for mounting rootfs,
        another line is for mounting system-boot partition.
        NOTE: slot id is the fslabel for slot's rootfs
        """
        logger.debug("update standby slot fstab file...")
        try:
            _fstab_fpath = self._mp_control.standby_slot_mount_point / Path(
                cfg.FSTAB_FPATH
            ).relative_to("/")
            _updated_fstab_str = Template(_FSTAB_TEMPLATE_STR).substitute(
                rootfs_fslabel=self._rpiboot_control.standby_slot
            )
            logger.debug(f"{_updated_fstab_str=}")
            write_str_to_file_sync(_fstab_fpath, _updated_fstab_str)
        except Exception as e:
            _err_msg = f"failed to update fstab file for standby slot: {e!r}"
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
            self._rpiboot_control.prepare_standby_dev(erase_standby=erase_standby)
            self._mp_control.mount_standby()
            self._mp_control.mount_active()

            ### update standby slot's ota_status files ###
            self._ota_status_control.pre_update_standby(version=version)
        except OTAError:
            raise
        except Exception as e:
            _err_msg = f"failed on pre_update: {e!r}"
            logger.error(_err_msg)
            raise BootControlPreUpdateFailed(_err_msg) from e

    def pre_rollback(self):
        try:
            logger.info("rpi_boot: pre-rollback setup...")
            self._ota_status_control.pre_rollback_current()
            self._mp_control.mount_standby()
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

    def post_update(self) -> Generator[None, None, None]:
        try:
            logger.info("rpi_boot: post-update setup...")
            self._copy_kernel_for_standby_slot()
            self._mp_control.preserve_ota_folder_to_standby()
            self._write_standby_fstab()
            self._rpiboot_control.prepare_tryboot_txt()
            self._mp_control.umount_all(ignore_error=True)
            yield  # hand over control back to otaclient
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
