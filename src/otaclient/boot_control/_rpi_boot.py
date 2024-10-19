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
"""Boot control support for Raspberry pi 4 Model B."""


from __future__ import annotations

import contextlib
import logging
import os
import subprocess
from pathlib import Path
from string import Template
from typing import Any, Generator, Literal

from typing_extensions import Self

import otaclient.errors as ota_errors
from otaclient._types import OTAStatus
from otaclient_common._io import copyfile_atomic, write_str_to_file_atomic
from otaclient_common.linux import subprocess_run_wrapper
from otaclient_common.typing import StrOrPath

from ._common import (
    CMDHelperFuncs,
    OTAStatusFilesControl,
    SlotMountHelper,
)
from .configs import rpi_boot_cfg as cfg
from .protocol import BootControllerProtocol

logger = logging.getLogger(__name__)


# ------ types ------ #
class SlotID(str):
    """slot_id for A/B slots."""

    VALID_SLOTS = ["slot_a", "slot_b"]

    def __new__(cls, _in: str | Self) -> Self:
        if isinstance(_in, cls):
            return _in
        if _in in cls.VALID_SLOTS:
            return str.__new__(cls, _in)
        raise ValueError(f"{_in=} is not valid slot num, should be {cls.VALID_SLOTS=}")


class _RPIBootControllerError(Exception):
    """rpi_boot module internal used exception."""


# ------ consts ------ #
CONFIG_TXT = "config.txt"  # primary boot cfg
TRYBOOT_TXT = "tryboot.txt"  # tryboot boot cfg
VMLINUZ = "vmlinuz"
INITRD_IMG = "initrd.img"
CMDLINE_TXT = "cmdline.txt"

SYSTEM_BOOT_FSLABEL = "system-boot"
SLOT_A = SlotID("slot_a")
SLOT_B = SlotID("slot_b")
AB_FLIPS = {SLOT_A: SLOT_B, SLOT_B: SLOT_A}
SEP_CHAR = "_"
"""separator between boot files name and slot suffix."""

_FSTAB_TEMPLATE_STR = (
    "LABEL=${rootfs_fslabel}\t/\text4\tdiscard,x-systemd.growfs\t0\t1\n"
    "LABEL=system-boot\t/boot/firmware\tvfat\tdefaults\t0\t1\n"
)


# ------ helper functions ------ #
BOOTFILES = Literal["vmlinuz", "initrd.img", "config.txt", "tryboot.txt", "cmdline.txt"]


def get_sysboot_files_fpath(boot_fname: BOOTFILES, slot: SlotID) -> Path:
    """Get the boot files fpath for specific slot from /boot/firmware.

    For example, for vmlinuz for slot_a, we get /boot/firmware/vmlinuz_slot_a
    """
    fname = f"{boot_fname}{SEP_CHAR}{slot}"
    return Path(cfg.SYSTEM_BOOT_MOUNT_POINT) / fname


class _RPIBootControl:
    """Boot control helper for rpi4 support.

    Supported partition layout:
        /dev/sd<x>:
            - sd<x>1: fat32, fslabel=systemb-boot
            - sd<x>2: ext4, fslabel=slot_a
            - sd<x>3: ext4, fslabel=slot_b
    slot_id is also the fslabel for each AB rootfs.
    NOTE that we allow extra partitions with ID after 3.

    Boot files for each slot have the following naming format:
        <boot_file_fname><sep_char><slot>
    For example, config.txt for slot_a will be config.txt_slot_a
    """

    def __init__(self) -> None:
        self.system_boot_mp = Path(cfg.SYSTEM_BOOT_MOUNT_POINT)
        self.system_boot_mp.mkdir(exist_ok=True)

        # sanity check, ensure we are running at raspberry pi device
        model_fpath = Path(cfg.RPI_MODEL_FILE)
        err_not_rpi_device = f"{cfg.RPI_MODEL_FILE} doesn't exist! Are we running at raspberry pi device?"
        if not model_fpath.is_file():
            logger.error(err_not_rpi_device)
            raise _RPIBootControllerError(err_not_rpi_device)

        model_info = model_fpath.read_text()
        if model_info.find("Pi") == -1:
            raise _RPIBootControllerError(err_not_rpi_device)
        logger.info(f"{model_info=}")

        try:
            # ------ detect active slot ------ #
            active_slot_dev = CMDHelperFuncs.get_current_rootfs_dev()
            assert active_slot_dev
            self.active_slot_dev = active_slot_dev
        except Exception as e:
            _err_msg = f"failed to detect current rootfs device: {e!r}"
            logger.error(_err_msg)
            raise _RPIBootControllerError(_err_msg) from e

        try:
            # detect the parent device of boot device
            #   i.e., for /dev/sda2 here we get /dev/sda
            parent_dev = CMDHelperFuncs.get_parent_dev(str(self.active_slot_dev))

            # get device tree, for /dev/sda device, we will get:
            #   ["/dev/sda", "/dev/sda1", "/dev/sda2", "/dev/sda3"]
            device_tree = CMDHelperFuncs.get_device_tree(parent_dev)
            logger.info(device_tree)

            # NOTE that we allow extra partitions presented after sd<x>3.
            assert len(device_tree) >= 4, "need at least 3 partitions"
        except Exception as e:
            _err_msg = f"failed to detect partition layout: {e!r}"
            logger.error(_err_msg)
            raise _RPIBootControllerError(_err_msg) from None

        # check system-boot partition mount
        system_boot_partition = device_tree[1]
        if not CMDHelperFuncs.is_target_mounted(
            self.system_boot_mp, raise_exception=False
        ):
            _err_msg = f"system-boot is not mounted at {self.system_boot_mp}, try to mount it..."
            logger.warning(_err_msg)

            try:
                CMDHelperFuncs.mount(
                    system_boot_partition,
                    self.system_boot_mp,
                    options=["defaults"],
                )
            except subprocess.CalledProcessError as e:
                _err_msg = (
                    f"failed to mount system-boot partition: {e!r}, {e.stderr.decode()}"
                )
                logger.error(_err_msg)
                raise _RPIBootControllerError(_err_msg) from e

        # check slots
        # sd<x>2 and sd<x>3
        rootfs_partitions = device_tree[2:4]

        # get the active slot ID by its position in the disk
        try:
            idx = rootfs_partitions.index(active_slot_dev)
        except ValueError:
            raise _RPIBootControllerError(
                f"active slot dev not found: {active_slot_dev=}, {rootfs_partitions=}"
            ) from None

        if idx == 0:  # slot_a
            self.active_slot = SLOT_A
            self.standby_slot = SLOT_B
            self.standby_slot_dev = rootfs_partitions[1]
        elif idx == 1:  # slot_b
            self.active_slot = SLOT_B
            self.standby_slot = SLOT_A
            self.standby_slot_dev = rootfs_partitions[0]

        logger.info(
            f"rpi_boot: active_slot: {self.active_slot}({self.active_slot_dev}), "
            f"standby_slot: {self.standby_slot}({self.standby_slot_dev})"
        )

        # ------ continue rpi_boot starts up ------ #
        self._check_boot_files()
        self._check_active_slot_id()

        # NOTE(20240604): for backward compatibility, always remove flag file
        flag_file = Path(cfg.SYSTEM_BOOT_MOUNT_POINT) / cfg.SWITCH_BOOT_FLAG_FILE
        flag_file.unlink(missing_ok=True)

    def _check_active_slot_id(self):
        """Check whether the active slot fslabel is matching the slot id.

        If mismatched, try to correct the problem.
        """
        fslabel = self.active_slot
        actual_fslabel = CMDHelperFuncs.get_attrs_by_dev(
            "LABEL", self.active_slot_dev, raise_exception=False
        )
        if actual_fslabel == fslabel:
            return

        logger.warning(
            (
                f"current active slot is {fslabel}, but its fslabel is {actual_fslabel}, "
                f"try to correct the fslabel with slot id {fslabel}..."
            )
        )
        try:
            CMDHelperFuncs.set_ext4_fslabel(self.active_slot_dev, fslabel)
            os.sync()
        except subprocess.CalledProcessError as e:
            logger.error(
                f"failed to correct the fslabel mismatched: {e!r}, {e.stderr.decode()}"
            )
            logger.error("this might cause problem on future OTA update!")

    def _check_boot_files(self):
        """Check the availability of boot files.

        The following boot files will be checked:
        1. config.txt_<slot_suffix> (required)
        2. cmdline.txt_<slot_suffix> (required)

        If any of the required files are missing, BootControlInitError will be raised.
        In such case, a reinstall/setup of AB partition boot files is required.
        """
        logger.debug("checking boot files...")
        active_slot, standby_slot = self.active_slot, self.standby_slot

        # ------ check active slot boot files ------ #
        config_txt_active_slot = get_sysboot_files_fpath(CONFIG_TXT, active_slot)
        if not config_txt_active_slot.is_file():
            _err_msg = f"missing {config_txt_active_slot=}"
            logger.error(_err_msg)
            raise _RPIBootControllerError(_err_msg)

        cmdline_txt_active_slot = get_sysboot_files_fpath(CMDLINE_TXT, active_slot)
        if not cmdline_txt_active_slot.is_file():
            _err_msg = f"missing {cmdline_txt_active_slot=}"
            logger.error(_err_msg)
            raise _RPIBootControllerError(_err_msg)

        # ------ check standby slot boot files ------ #
        config_txt_standby_slot = get_sysboot_files_fpath(CONFIG_TXT, standby_slot)
        if not config_txt_standby_slot.is_file():
            _err_msg = f"missing {config_txt_standby_slot=}"
            logger.error(_err_msg)
            raise _RPIBootControllerError(_err_msg)

        cmdline_txt_standby_slot = get_sysboot_files_fpath(CMDLINE_TXT, standby_slot)
        if not cmdline_txt_standby_slot.is_file():
            _err_msg = f"missing {cmdline_txt_standby_slot=}"
            logger.error(_err_msg)
            raise _RPIBootControllerError(_err_msg)

    @staticmethod
    @contextlib.contextmanager
    def _prepare_flash_kernel(target_slot_mp: StrOrPath) -> Generator[None, Any, None]:
        """Do a bind mount of /boot/firmware, /proc and /sys to the standby slot,
        preparing for calling flash-kernel with chroot.

        flash-kernel requires at least these mounts to work properly.
        """
        target_slot_mp = Path(target_slot_mp)
        mounts: dict[str, str] = {}

        # we need to mount /proc, /sys and /boot/firmware to make flash-kernel works
        system_boot_mp = target_slot_mp / Path(cfg.SYSTEM_BOOT_MOUNT_POINT).relative_to(
            "/"
        )
        mounts[str(system_boot_mp)] = cfg.SYSTEM_BOOT_MOUNT_POINT

        proc_mp = target_slot_mp / "proc"
        mounts[str(proc_mp)] = "/proc"

        sys_mp = target_slot_mp / "sys"
        mounts[str(sys_mp)] = "/sys"

        try:
            for _mp, _src in mounts.items():
                CMDHelperFuncs.mount(
                    _src,
                    _mp,
                    options=["bind"],
                    params=["--make-unbindable"],
                )
            yield
            # NOTE: passthrough the mount failure to caller
        finally:
            for _mp in mounts:
                CMDHelperFuncs.umount(_mp, raise_exception=False)

    def update_firmware(self, *, target_slot: SlotID, target_slot_mp: StrOrPath):
        """Call flash-kernel to install new dtb files, boot firmwares and kernel, initrd.img
        from target slot.

        The following things will be done:
        1. bind mount the /boot/firmware and /proc into the target slot.
        2. chroot into the target slot's rootfs, execute flash-kernel
        """
        logger.info(f"try to flash-kernel from {target_slot}...")
        try:
            with self._prepare_flash_kernel(target_slot_mp):
                subprocess_run_wrapper(
                    ["/usr/sbin/flash-kernel"],
                    check=True,
                    check_output=True,
                    chroot=target_slot_mp,
                    # must set this env variable to make flash-kernel work under chroot
                    env={"FK_FORCE": "yes"},
                )
                os.sync()
        except subprocess.CalledProcessError as e:
            _err_msg = f"flash-kernel failed: {e!r}\nstderr: {e.stderr.decode()}\nstdout: {e.stdout.decode()}"
            logger.error(_err_msg)
            raise _RPIBootControllerError(_err_msg) from None

        try:
            # flash-kernel will install the kernel and initrd.img files from /boot to /boot/firmware
            if (vmlinuz := self.system_boot_mp / VMLINUZ).is_file():
                os.replace(
                    vmlinuz,
                    get_sysboot_files_fpath(VMLINUZ, target_slot),
                )

            if (initrd_img := self.system_boot_mp / INITRD_IMG).is_file():
                os.replace(
                    initrd_img,
                    get_sysboot_files_fpath(INITRD_IMG, target_slot),
                )

            # NOTE(20240603): for backward compatibility(downgrade), still create the flag file.
            #   The present of flag files means the firmware is updated.
            flag_file = Path(cfg.SYSTEM_BOOT_MOUNT_POINT) / cfg.SWITCH_BOOT_FLAG_FILE
            flag_file.write_text("")
            os.sync()
        except Exception as e:
            _err_msg = f"failed to apply new kernel,initrd.img for {target_slot}: {e!r}"
            logger.error(_err_msg)
            raise _RPIBootControllerError(_err_msg) from None

    # exposed API methods/properties

    def finalize_switching_boot(self) -> bool:
        """Finalize switching boot by swapping config.txt and tryboot.txt if we should.

        Finalize switch boot:
            1. atomically replace tryboot.txt with tryboot.txt_<standby_slot_id>
            2. atomically replace config.txt with config.txt_<active_slot_id>

        Returns:
            A bool indicates whether the switch boot succeeded or not. Note that no exception
                will be raised if finalizing failed.
        """
        logger.info("finalizing switch boot...")
        current_slot, standby_slot = self.active_slot, self.standby_slot
        config_txt_current = get_sysboot_files_fpath(CONFIG_TXT, current_slot)
        config_txt_standby = get_sysboot_files_fpath(CONFIG_TXT, standby_slot)

        try:
            copyfile_atomic(config_txt_current, self.system_boot_mp / CONFIG_TXT)
            copyfile_atomic(config_txt_standby, self.system_boot_mp / TRYBOOT_TXT)
            logger.info(
                "finalizing boot configuration,"
                f"replace {CONFIG_TXT} with {config_txt_current=}, "
                f"replace {TRYBOOT_TXT} with {config_txt_standby=}"
            )

            # on success switching boot, cleanup the bak files created by flash-kernel
            for _bak_file in self.system_boot_mp.glob("**/*.bak"):
                _bak_file.unlink(missing_ok=True)
            return True
        except Exception as e:
            _err_msg = f"failed to finalize boot switching: {e!r}"
            logger.error(_err_msg)
            return False

    def prepare_tryboot_txt(self):
        """Copy the standby slot's config.txt as tryboot.txt."""
        try:
            copyfile_atomic(
                get_sysboot_files_fpath(CONFIG_TXT, self.standby_slot),
                self.system_boot_mp / TRYBOOT_TXT,
            )
            logger.info(f"set {TRYBOOT_TXT} as {self.standby_slot}'s one")
        except Exception as e:
            _err_msg = f"failed to prepare tryboot.txt for {self.standby_slot}"
            logger.error(_err_msg)
            raise _RPIBootControllerError(_err_msg) from e

    def reboot_tryboot(self):
        """Reboot with tryboot flag."""
        logger.info(f"tryboot reboot to standby slot({self.standby_slot})...")
        try:
            # NOTE: "0 tryboot" is a single param.
            CMDHelperFuncs.reboot(args=["0 tryboot"])
        except Exception as e:
            _err_msg = "failed to reboot"
            logger.exception(_err_msg)
            raise _RPIBootControllerError(_err_msg) from e


class RPIBootController(BootControllerProtocol):
    """BootControllerProtocol implementation for rpi4 support."""

    def __init__(self) -> None:
        try:
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
            logger.info("rpi_boot starting finished")
        except Exception as e:
            _err_msg = f"failed to start rpi boot controller: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlStartupFailed(_err_msg, module=__name__) from e

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
            write_str_to_file_atomic(_fstab_fpath, _updated_fstab_str)
        except Exception as e:
            _err_msg = f"failed to update fstab file for standby slot: {e!r}"
            logger.error(_err_msg)
            raise _RPIBootControllerError(_err_msg) from e

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
            self._mp_control.prepare_standby_dev(
                erase_standby=erase_standby,
                fslabel=self._rpiboot_control.standby_slot,
            )
            self._mp_control.mount_standby()
            self._mp_control.mount_active()

            ### update standby slot's ota_status files ###
            self._ota_status_control.pre_update_standby(version=version)
        except Exception as e:
            _err_msg = f"failed on pre_update: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPreUpdateFailed(
                _err_msg, module=__name__
            ) from e

    def pre_rollback(self):
        try:
            logger.info("rpi_boot: pre-rollback setup...")
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
            logger.info("rpi_boot: post-rollback setup...")
            self._rpiboot_control.prepare_tryboot_txt()
            self._mp_control.umount_all(ignore_error=True)
            self._rpiboot_control.reboot_tryboot()
        except Exception as e:
            _err_msg = f"failed on post_rollback: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPostRollbackFailed(
                _err_msg, module=__name__
            ) from e

    def post_update(self) -> Generator[None, None, None]:
        try:
            logger.info("rpi_boot: post-update setup...")
            self._mp_control.preserve_ota_folder_to_standby()
            self._write_standby_fstab()
            self._rpiboot_control.update_firmware(
                target_slot=self._rpiboot_control.standby_slot,
                target_slot_mp=self._mp_control.standby_slot_mount_point,
            )
            self._rpiboot_control.prepare_tryboot_txt()
            self._mp_control.umount_all(ignore_error=True)
            yield  # hand over control back to otaclient
            self._rpiboot_control.reboot_tryboot()
        except Exception as e:
            _err_msg = f"failed on post_update: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPostUpdateFailed(
                _err_msg, module=__name__
            ) from e

    def on_operation_failure(self):
        """Failure registering and cleanup at failure."""
        logger.warning("on failure try to unmounting standby slot...")
        self._ota_status_control.on_failure()
        self._mp_control.umount_all(ignore_error=True)

    def load_version(self) -> str:
        return self._ota_status_control.load_active_slot_version()

    def get_booted_ota_status(self) -> OTAStatus:
        return self._ota_status_control.booted_ota_status
