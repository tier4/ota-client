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
import os
import re
from pathlib import Path
from functools import partial
from typing import Generator, Literal, NoReturn
from otaclient._utils.path import replace_root

from otaclient._utils.subprocess import (
    compose_cmd,
    subprocess_call,
    subprocess_check_output,
    SubProcessCallFailed,
)
from otaclient._utils.linux import DEFAULT_NS_TO_ENTER

from .. import log_setting, errors as ota_errors
from ..configs import config as cfg
from ..common import copytree_identical, read_str_from_file, write_str_to_file_sync
from ..proto import wrapper

from .._cmdhelpers import (
    gen_partuuid_str,
    get_dev_partuuid,
    get_current_rootfs_dev,
    log_subprocess_exec,
    mount_rw,
    reboot,
    take_arg,
    umount,
)
from ._common import (
    prepare_standby_slot_dev_ext4,
    OTAStatusFilesControl,
    SlotMountHelper,
)
from .configs import cboot_cfg as boot_cfg
from .protocol import BootControllerProtocol
from .firmware import Firmware


logger = log_setting.get_logger(__name__)


class _NvbootctrlError(Exception):
    """Specific internal errors related to nvbootctrl cmd."""


@take_arg(subprocess_check_output)
def _nvbootctrl(_args: str, **kwargs) -> str | None:
    _cmd = compose_cmd("nvbootctrl", _args)
    logger.debug(f"cmd execute: {_cmd}")
    return subprocess_check_output(_cmd, **kwargs)


@take_arg(subprocess_call)
def _nvbootctrl_check_return_code(_args: str, **kwargs) -> str | None:
    _cmd = compose_cmd("nvbootctrl", _args)
    logger.debug(f"cmd execute: {_cmd}")
    return subprocess_call(_cmd, **kwargs)


class Nvbootctrl:
    """
    NOTE: slot and rootfs are binding accordingly!
          partid mapping: p1->slot0, p2->slot1

    slot num: 0->A, 1->B
    """

    TARGET_TYPE = Literal["rootfs", "bootloader"]
    EMMC_DEV = "mmcblk0"
    NVME_DEV = "nvme0n1"

    # slot0<->slot1
    CURRENT_STANDBY_FLIP = {"0": "1", "1": "0"}
    # p1->slot0, p2->slot1
    PARTID_SLOTID_MAP = {"1": "0", "2": "1"}
    # slot0->p1, slot1->p2
    SLOTID_PARTID_MAP = {v: k for k, v in PARTID_SLOTID_MAP.items()}

    @staticmethod
    def _nvbootctrl(
        args: str, *, target: TARGET_TYPE, raise_exception=True
    ) -> str | None:
        """
        Raises:
            SubProcessCalledFailed if raise_exception is True.
        """
        _args = f"-t {target} {args}"
        with log_subprocess_exec(
            logger.warning, f"nvbootctrl command({_args=}) execution failed"
        ):
            return _nvbootctrl(
                _args,
                enter_root_ns=DEFAULT_NS_TO_ENTER if cfg.IS_CONTAINER else None,
                raise_exception=raise_exception,
            )

    @classmethod
    def get_current_slot(cls, *, target: TARGET_TYPE = "rootfs") -> str:
        try:
            slot = cls._nvbootctrl("get-current-slot", target=target)
            assert slot
            return slot
        except (SubProcessCallFailed, AssertionError) as e:
            _err_msg = f"failed to get current slot: {e!r}"
            logger.error(_err_msg)
            raise _NvbootctrlError(_err_msg) from e

    @classmethod
    def dump_slots_info(cls, *, target: TARGET_TYPE = "rootfs") -> str | None:
        return cls._nvbootctrl("dump-slots-info", raise_exception=False, target=target)

    @classmethod
    def mark_boot_successful(cls, *, target: TARGET_TYPE = "rootfs") -> None:
        """Mark current slot as boot successfully."""
        try:
            cls._nvbootctrl("mark-boot-successful", target=target)
        except SubProcessCallFailed as e:
            _err_msg = f"failed mark current slot as boot successful: {e!r}"
            logger.error(_err_msg)
            raise _NvbootctrlError(_err_msg) from e

    @classmethod
    def set_active_boot_slot(cls, slot: str, *, target: TARGET_TYPE = "rootfs"):
        try:
            cls._nvbootctrl(f"set-active-boot-slot {slot}", target=target)
        except SubProcessCallFailed as e:
            _err_msg = f"failed to set active rootfs slot to {slot=}: {e!r}"
            logger.error(_err_msg)
            raise _NvbootctrlError(_err_msg) from e

    @classmethod
    def set_slot_as_unbootable(cls, slot: str, *, target: TARGET_TYPE = "rootfs"):
        try:
            cls._nvbootctrl(f"set-slot-as-unbootable {slot}", target=target)
        except (SubProcessCallFailed, AssertionError) as e:
            _err_msg = f"failed to set {slot} as unbootable: {e!r}"
            logger.error(_err_msg)
            raise _NvbootctrlError(_err_msg) from e

    @classmethod
    def is_slot_bootable(cls, slot: str, *, target="rootfs") -> bool:
        try:
            # NOTE: nvbootctrl will return non-zero code on unbootable,
            #       zero code on bootable.
            _nvbootctrl_check_return_code(
                f"is-slot-bootable -t {target} {slot}", raise_exception=True
            )
            return True
        except SubProcessCallFailed:
            return False

    @classmethod
    def is_slot_marked_successful(cls, slot: str) -> bool:
        try:
            # NOTE: nvbootctrl will return non-zero code on failed slot,
            #       zero code on succeeded slot.
            _nvbootctrl_check_return_code(
                f"is-slot-marked-successful {slot}", raise_exception=True
            )
            return True
        except SubProcessCallFailed:
            return False


class _CBootControl:
    def __init__(self):
        self._check_tegra_chip_id()
        self._init_dev_info()
        logger.info(f"finished cboot control init: {Nvbootctrl.dump_slots_info()=}")

    def _check_tegra_chip_id(self) -> None:
        """Check whether the device is supported by CBootController.

        NOTE: only support rqx-580, rqx-58g platform right now!

        Raises:
            NotImplementedError if chip_id is invalid or not in otaclient
                cboot controller support list.
        """
        _raw_chip_id = read_str_from_file(boot_cfg.TEGRA_CHIP_ID_FPATH)
        try:
            chip_id = int(_raw_chip_id)
        except ValueError:
            raise NotImplementedError(f"invalid chip_id: {_raw_chip_id=}")

        if chip_id not in boot_cfg.CHIP_ID_MODEL_MAP:
            raise NotImplementedError(f"unsupported platform found ({chip_id=}), abort")

        model = boot_cfg.CHIP_ID_MODEL_MAP[chip_id]
        logger.info(f"cboot loaded for device {model=}, (chip_id={hex(chip_id)})")

    def _init_dev_info(self):
        self._current_slot: str = Nvbootctrl.get_current_slot()

        try:
            _current_rootfs_dev = get_current_rootfs_dev(raise_exception=True)
            assert _current_rootfs_dev
        except (SubProcessCallFailed, AssertionError) as e:
            _err_msg = f"failed to get current root dev: {e!r}"
            logger.error(_err_msg)
            raise _NvbootctrlError(_err_msg) from e
        self._current_rootfs_dev: str = _current_rootfs_dev

        # NOTE: boot dev is always emmc device now
        self._current_boot_dev: str = f"/dev/{Nvbootctrl.EMMC_DEV}p{Nvbootctrl.SLOTID_PARTID_MAP[self._current_slot]}"

        self._standby_slot: str = Nvbootctrl.CURRENT_STANDBY_FLIP[self._current_slot]
        standby_partid = Nvbootctrl.SLOTID_PARTID_MAP[self._standby_slot]
        self._standby_boot_dev: str = f"/dev/{Nvbootctrl.EMMC_DEV}p{standby_partid}"

        #
        # ------detect standby slot rootfs device ------ #
        #
        is_rootfs_on_external = False
        try:
            if self._current_rootfs_dev.find(Nvbootctrl.NVME_DEV) != -1:
                logger.info(
                    "rootfs on nvme storage detected, using nvme storage as rootfs"
                )
                is_rootfs_on_external = True
                standby_rootfs_dev = f"/dev/{Nvbootctrl.NVME_DEV}p{standby_partid}"

                standby_slot_partuuid = get_dev_partuuid(
                    standby_rootfs_dev, raise_exception=True
                )
                assert standby_slot_partuuid

            elif self._current_rootfs_dev.find(Nvbootctrl.EMMC_DEV) != -1:
                logger.info("using internal emmc storage as rootfs")
                standby_rootfs_dev = f"/dev/{Nvbootctrl.EMMC_DEV}p{standby_partid}"

                standby_slot_partuuid = get_dev_partuuid(
                    standby_rootfs_dev, raise_exception=True
                )
                assert standby_slot_partuuid

            else:
                raise NotImplementedError(
                    f"rootfs on {self._current_rootfs_dev} is not supported, abort"
                )

        except AssertionError:
            _err_msg = "failed to get standby slot partuuid"
            logger.error(_err_msg)
            raise _NvbootctrlError(_err_msg) from None
        except SubProcessCallFailed as e:
            _err_msg = (
                f"failed to detect standby slot rootfs device: {_current_rootfs_dev=}"
            )
            logger.error(_err_msg)
            raise _NvbootctrlError(_err_msg) from e

        self.is_rootfs_on_external = is_rootfs_on_external
        self._standby_rootfs_dev = standby_rootfs_dev
        self._standby_slot_partuuid = standby_slot_partuuid

        logger.info("dev info initializing completed")
        logger.info(
            f"{self._current_slot=}, {self._current_boot_dev=}, {self._current_rootfs_dev=}\n"
            f"{self._standby_slot=}, {self._standby_boot_dev=}, {self._standby_rootfs_dev=}"
        )

    ###### CBootControl API ######

    @property
    def active_slot(self) -> str:
        return self._current_slot

    @property
    def standby_slot(self) -> str:
        return self._standby_slot

    @property
    def active_slot_dev(self) -> str:
        return self._current_rootfs_dev

    @property
    def standby_slot_dev(self) -> str:
        return self._standby_rootfs_dev

    @property
    def active_slot_boot_dev(self) -> str:
        return self._current_boot_dev

    @property
    def standby_slot_boot_dev(self) -> str:
        return self._standby_boot_dev

    @property
    def standby_slot_dev_partuuid(self) -> str:
        return self._standby_slot_partuuid

    def mark_current_slot_boot_successful(self):
        logger.info(f"mark {self._current_slot=} as boot successful")
        Nvbootctrl.mark_boot_successful()

    def set_standby_slot_unbootable(self):
        slot = self._standby_slot
        Nvbootctrl.set_slot_as_unbootable(slot)

    def switch_boot_to(self, slot: str):
        logger.info(f"switch boot to {slot=}")
        Nvbootctrl.set_active_boot_slot(slot, target="bootloader")
        Nvbootctrl.set_active_boot_slot(slot)

    def finalize_switching_boot(self) -> bool:
        logger.info("finalizing switch boot...")
        try:
            self.mark_current_slot_boot_successful()
            return True
        except Exception as e:
            _err_msg = f"failed to finalize boot switching: {e!r}"
            logger.error(_err_msg)
            return False

    def is_current_slot_marked_successful(self) -> bool:
        slot = self._current_slot
        return Nvbootctrl.is_slot_marked_successful(slot)

    @staticmethod
    def update_extlinux_cfg(dst: Path, ref: Path, partuuid_str: str):
        """Write dst extlinux.conf based on reference extlinux.conf and partuuid_str.

        Params:
            dst: path to dst extlinux.conf file
            ref: reference extlinux.conf file
            partuuid_str: rootfs specification string like "PARTUUID=<partuuid>"
        """

        def _replace(ma: re.Match, repl: str):
            append_l: str = ma.group(0)
            if append_l.startswith("#"):
                return append_l
            res, n = re.compile(r"root=[\w\-=]*").subn(repl, append_l)
            if not n:
                res = f"{append_l} {repl}"

            return res

        _repl_func = partial(_replace, repl=f"root={partuuid_str}")
        write_str_to_file_sync(
            dst, re.compile(r"\n\s*APPEND.*").sub(_repl_func, ref.read_text())
        )


class CBootController(BootControllerProtocol):
    def __init__(self) -> None:
        try:
            self._cboot_control = cboot_control = _CBootControl()

            # ------ prepare mount space ------ #
            otaclient_ms = Path(cfg.OTACLIENT_MOUNT_SPACE_DPATH)
            otaclient_ms.mkdir(exist_ok=True, parents=True)
            otaclient_ms.chmod(0o700)

            self._mp_control = SlotMountHelper(
                standby_slot_dev=cboot_control.standby_slot_dev,
                active_slot_dev=cboot_control.active_slot_dev,
            )

            self._ota_status_control = OTAStatusFilesControl(
                active_slot=cboot_control.active_slot,
                standby_slot=cboot_control.standby_slot,
                current_ota_status_dir=Path(boot_cfg.ACTIVE_BOOT_OTA_STATUS_DPATH),
                # NOTE: might not yet be populated before OTA update applied!
                standby_ota_status_dir=Path(boot_cfg.STANDBY_BOOT_OTA_STATUS_DPATH),
                finalize_switching_boot=cboot_control.finalize_switching_boot,
            )
        except NotImplementedError as e:
            _err_msg = f"failed to start cboot controller: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPlatformUnsupported(
                _err_msg, module=__name__
            ) from e
        except Exception as e:
            _err_msg = f"failed to start cboot controller: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlStartupFailed(_err_msg, module=__name__) from e

    ###### private methods ######

    def _standby_slot_populate_boot_folder_to_separate_bootdev(self):
        """Populate /boot folder under standby slot's rootfs to separate bootdev.

        This method will be called if rootfs on external storage is enabled.
        """
        # mount the actual standby_boot_dev now
        _boot_dir_mount_point = Path(boot_cfg.SEPARATE_BOOT_MOUNT_POINT)
        _boot_dir_mount_point.mkdir(exist_ok=True, parents=True)

        # NOTE(20240124): mount related operation MUST always use canonical path
        _canonical_boot_dir_mp = replace_root(
            boot_cfg.SEPARATE_BOOT_MOUNT_POINT,
            cfg.ACTIVE_ROOTFS,
            cfg.DEFAULT_ACTIVE_ROOTFS,
        )

        try:
            mount_rw(
                self._cboot_control.standby_slot_boot_dev,
                _canonical_boot_dir_mp,
            )
        except SubProcessCallFailed as e:
            _err_msg = f"failed to mount standby boot dev: {e!r}"
            logger.error(_err_msg)
            raise _NvbootctrlError(_err_msg) from e

        try:
            dst = _boot_dir_mount_point / "boot"
            dst.mkdir(exist_ok=True, parents=True)
            src = self._mp_control.standby_slot_mount_point / "boot"

            # copy the standby slot's boot folder to emmc boot dev
            copytree_identical(src, dst)
        except Exception as e:
            _err_msg = f"failed to populate boot folder to separate bootdev: {e!r}"
            logger.error(_err_msg)
            raise _NvbootctrlError(_err_msg) from e

        finally:
            # unmount standby emmc boot dev on finish/failure
            # NOTE(20240124): umount operation MUST always use canonical path
            try:
                umount(_canonical_boot_dir_mp)
            except Exception as e:
                _failure_msg = f"failed to umount boot dev: {e!r}"
                logger.warning(_failure_msg)
                # no need to raise to the caller

    # APIs

    def on_operation_failure(self):
        """Failure registering and cleanup at failure."""
        logger.warning("on failure try to unmounting standby slot...")
        self._ota_status_control.on_failure()
        self._mp_control.umount_all()

    def get_standby_slot_path(self) -> Path:
        return self._mp_control.standby_slot_mount_point

    def get_standby_boot_dir(self) -> Path:
        """
        NOTE: in cboot controller, we directly use the /boot dir under the standby slot,
              and sync to the external boot dev in the post_update if needed.
        """
        return self._mp_control.standby_slot_mount_point / "boot"

    def pre_update(self, version: str, *, standby_as_ref: bool, erase_standby: bool):
        logger.info("cboot: pre-update setup...")
        try:
            self._cboot_control.set_standby_slot_unbootable()
            # update active slot's ota_status
            self._ota_status_control.pre_update_current()

            # prepare and mount standby slot
            prepare_standby_slot_dev_ext4(
                self._cboot_control.standby_slot_dev,
                erase_standby=erase_standby,
            )
            self._mp_control.mount_standby_slot_dev()

            self._mp_control.mount_active_slot_dev()

            # re-populate /boot/ota-status folder for standby slot
            self._ota_status_control.standby_ota_status_dir.mkdir(
                exist_ok=True, parents=True
            )
            # update standby slot's ota_status files
            self._ota_status_control.pre_update_standby(version=version)
        except Exception as e:
            _err_msg = f"failed on pre_update: {e!r}"
            logger.exception(_err_msg)
            raise ota_errors.BootControlPreUpdateFailed(
                f"{e!r}", module=__name__
            ) from e

    def post_update(self) -> Generator[None, None, NoReturn]:
        try:
            # firmware update
            firmware = Firmware(Path(boot_cfg.FIRMWARE_CFG_STANDBY_FPATH))
            firmware.update(int(self._cboot_control.standby_slot))
            os.sync()  # ensure changes applied

            # update extlinux_cfg file
            _extlinux_cfg = Path(boot_cfg.STANDBY_EXTLINUX_FPATH)
            self._cboot_control.update_extlinux_cfg(
                dst=_extlinux_cfg,
                ref=_extlinux_cfg,
                partuuid_str=gen_partuuid_str(
                    self._cboot_control.standby_slot_dev_partuuid,
                ),
            )

            # NOTE: we didn't prepare /boot/ota here,
            #       process_persistent does this for us
            if self._cboot_control.is_rootfs_on_external:
                logger.info(
                    "rootfs on external storage detected: "
                    "updating the /boot folder in standby bootdev..."
                )
                self._standby_slot_populate_boot_folder_to_separate_bootdev()

            logger.info("post update finished, rebooting...")
            self._mp_control.umount_all()

            # swith boot to standby slot
            self._cboot_control.switch_boot_to(self._cboot_control.standby_slot)
            logger.info(f"[post-update]: {Nvbootctrl.dump_slots_info()=}")

            yield  # hand over control back to otaclient

            reboot()  # otaclient will be terminated on succeeded call
        except Exception as e:
            _err_msg = f"failed on post_update: {e!r}"
            logger.exception(_err_msg)
            raise ota_errors.BootControlPostUpdateFailed(
                _err_msg, module=__name__
            ) from e

    def pre_rollback(self) -> None:
        logger.info("cboot: pre-rollback setup...")
        try:
            self._ota_status_control.pre_rollback_current()

            self._mp_control.mount_standby_slot_dev()
            self._ota_status_control.pre_rollback_standby()
        except Exception as e:
            _err_msg = f"failed on pre_rollback: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPreRollbackFailed(
                _err_msg, module=__name__
            ) from e

    def post_rollback(self) -> NoReturn:
        logger.info("cboot: post-rollback setup...")
        try:
            self._cboot_control.switch_boot_to(self._cboot_control.standby_slot)

            self._mp_control.umount_all()
            reboot()
        except Exception as e:
            _err_msg = f"failed on post_rollback: {e!r}"
            logger.exception(_err_msg)
            raise ota_errors.BootControlPostRollbackFailed(
                _err_msg, module=__name__
            ) from e

    def load_version(self) -> str:
        return self._ota_status_control.load_active_slot_version()

    def get_booted_ota_status(self) -> wrapper.StatusOta:
        return self._ota_status_control.booted_ota_status
