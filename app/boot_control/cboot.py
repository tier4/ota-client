import os
import re
from pathlib import Path
from functools import partial
from subprocess import CalledProcessError
from typing import Optional

from app import log_util
from app.boot_control.common import (
    MountError,
    OTAStatusMixin,
    _BootControlError,
    PrepareMountMixin,
    CMDHelperFuncs,
    SlotInUseMixin,
    VersionControlMixin,
)
from app.boot_control.interface import BootControllerProtocol
from app.common import (
    copytree_identical,
    read_from_file,
    subprocess_call,
    subprocess_check_output,
)
from app.configs import BOOT_LOADER, cboot_cfg as cfg
from app.errors import (
    BootControlInitError,
    BootControlPlatformUnsupported,
    BootControlPostRollbackFailed,
    BootControlPostUpdateFailed,
    BootControlPreUpdateFailed,
)
from app.ota_status import OTAStatusEnum

assert BOOT_LOADER == "cboot", f"ERROR, try to use cboot on {BOOT_LOADER}, abort"

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class NvbootctrlError(_BootControlError):
    """Specific internal errors related to nvbootctrl cmd."""


class Nvbootctrl:
    """
    NOTE: slot and rootfs are binding accordingly!
          partid mapping: p1->slot0, p2->slot1

    slot num: 0->A, 1->B
    """

    EMMC_DEV: str = "mmcblk0"
    NVME_DEV: str = "nvme0n1"
    # slot0<->slot1
    CURRENT_STANDBY_FLIP = {"0": "1", "1": "0"}
    # p1->slot0, p2->slot1
    PARTID_SLOTID_MAP = {"1": "0", "2": "1"}
    # slot0->p1, slot1->p2
    SLOTID_PARTID_MAP = {v: k for k, v in PARTID_SLOTID_MAP.items()}

    # nvbootctrl
    @staticmethod
    def _nvbootctrl(arg: str, *, call_only=True, raise_exception=True) -> Optional[str]:
        """
        Raises:
            NvbootCtrlError if raise_exception is True.
        """
        # NOTE: target is always set to rootfs
        _cmd = f"nvbootctrl -t rootfs {arg}"
        try:
            if call_only:
                subprocess_call(_cmd, raise_exception=raise_exception)
                return
            else:
                return subprocess_check_output(_cmd, raise_exception=raise_exception)
        except CalledProcessError as e:
            raise NvbootctrlError from e

    @classmethod
    def check_rootdev(cls, dev: str) -> bool:
        """
        check whether the givin dev is legal root dev or not

        NOTE: expect using UUID method to assign rootfs!
        """
        pa = re.compile(r"\broot=(?P<rdev>[\w=-]*)\b")
        try:
            if ma := pa.search(subprocess_check_output("cat /proc/cmdline")):
                res = ma.group("rdev")
            else:
                raise ValueError(f"failed to find specific {dev} in cmdline")
        except (CalledProcessError, ValueError) as e:
            raise NvbootctrlError(
                "rootfs detect failed or rootfs is not specified by PARTUUID in kernel cmdline"
            ) from e

        uuid = res.split("=")[-1]

        return Path(CMDHelperFuncs.get_dev_by_partuuid(uuid)).resolve(
            strict=True
        ) == Path(dev).resolve(strict=True)

    @classmethod
    def get_current_slot(cls) -> str:
        if slot := cls._nvbootctrl("get-current-slot", call_only=False):
            return slot
        else:
            raise NvbootctrlError

    @classmethod
    def mark_boot_successful(cls, slot: str):
        cls._nvbootctrl(f"mark-boot-successful {slot}")

    @classmethod
    def set_active_boot_slot(cls, slot: str):
        cls._nvbootctrl(f"set-active-boot-slot {slot}")

    @classmethod
    def set_slot_as_unbootable(cls, slot: str):
        cls._nvbootctrl(f"set-slot-as-unbootable {slot}")

    @classmethod
    def is_slot_bootable(cls, slot: str) -> bool:
        try:
            cls._nvbootctrl(f"is-slot-bootable {slot}")
            return True
        except NvbootctrlError:
            return False

    @classmethod
    def is_slot_marked_successful(cls, slot: str) -> bool:
        try:
            cls._nvbootctrl(f"is-slot-marked-successful {slot}")
            return True
        except NvbootctrlError:
            return False


class _CBootControl:
    KERNEL: str = "/boot/Image"
    KERNEL_SIG: str = "/boot/Image.sig"
    INITRD: str = "/boot/initrd"
    INITRD_IMG_LINK: str = "/boot/initrd.img"
    FDT: str = "/boot/tegra194-rqx-580.dtb"
    FDT_HDR40: str = "/boot/tegra194-rqx-580-hdr40.dtbo"
    EXTRA_CMDLINE: str = (
        "console=ttyTCU0,115200n8 console=tty0 fbcon=map:0 net.ifnames=0"
    )

    def __init__(self):
        self._linux = self.KERNEL
        self._initrd = self.INITRD
        self._fdt = self.FDT
        self._cmdline_extra = self.EXTRA_CMDLINE

        try:
            # NOTE: only support r580 platform right now!
            # detect the chip id
            tegra_chip_id_f = Path("/sys/module/tegra_fuse/parameters/tegra_chip_id")
            self.chip_id = read_from_file(tegra_chip_id_f)
            if self.chip_id == "" or int(self.chip_id) not in cfg.CHIP_ID_MODEL_MAP:
                raise NotImplementedError(
                    f"unsupported platform found (chip_id: {self.chip_id}), abort"
                )

            self.chip_id = int(self.chip_id)
            self.model = cfg.CHIP_ID_MODEL_MAP[self.chip_id]
            logger.info(f"{self.model=}, (chip_id={hex(self.chip_id)})")

            # initializing dev info
            self._init_dev_info()
        except NotImplementedError as e:
            raise BootControlPlatformUnsupported from e
        except Exception as e:
            raise BootControlInitError from e

    def _init_dev_info(self):
        self.current_slot: str = Nvbootctrl.get_current_slot()
        self.current_rootfs_dev: str = CMDHelperFuncs.get_current_rootfs_dev()
        # NOTE: boot dev is always emmc device now
        self.current_boot_dev: str = f"/dev/{Nvbootctrl.EMMC_DEV}p{Nvbootctrl.SLOTID_PARTID_MAP[self.current_slot]}"

        self.standby_slot: str = Nvbootctrl.CURRENT_STANDBY_FLIP[self.current_slot]
        standby_partid = Nvbootctrl.SLOTID_PARTID_MAP[self.standby_slot]
        self.standby_boot_dev: str = f"/dev/{Nvbootctrl.EMMC_DEV}p{standby_partid}"

        # detect rootfs position
        if self.current_rootfs_dev.find(Nvbootctrl.NVME_DEV) != -1:
            logger.debug("rootfs on external storage detected, nvme rootfs is enable")
            self.is_rootfs_on_external = True
            self.standby_rootfs_dev = f"/dev/{Nvbootctrl.NVME_DEV}p{standby_partid}"
            self.standby_slot_partuuid = CMDHelperFuncs.get_partuuid_str_by_dev(
                self.standby_rootfs_dev
            )
        elif self.current_rootfs_dev.find(Nvbootctrl.EMMC_DEV) != -1:
            logger.debug("using internal storage as rootfs")
            self.is_rootfs_on_external = False
            self.standby_rootfs_dev = f"/dev/{Nvbootctrl.EMMC_DEV}p{standby_partid}"
            self.standby_slot_partuuid = CMDHelperFuncs.get_partuuid_str_by_dev(
                self.standby_rootfs_dev
            )
        else:
            raise NotImplementedError(
                f"rootfs on {self.current_rootfs_dev} is not supported, abort"
            )

        # ensure rootfs is as expected
        if not Nvbootctrl.check_rootdev(self.current_rootfs_dev):
            msg = f"rootfs mismatch, expect {self.current_rootfs_dev} as rootfs"
            raise ValueError(msg)
        elif Nvbootctrl.check_rootdev(self.standby_rootfs_dev):
            msg = (
                f"rootfs mismatch, expect {self.standby_rootfs_dev} as standby slot dev"
            )
            raise ValueError(msg)

        logger.info("dev info initializing completed")
        logger.info(
            f"{self.current_slot=}, {self.current_boot_dev=}, {self.current_rootfs_dev=}"
        )
        logger.info(
            f"{self.standby_slot=}, {self.standby_boot_dev=}, {self.standby_rootfs_dev=}"
        )

    ###### CBootControl API ######
    def get_current_slot(self) -> str:
        return self.current_slot

    def get_current_rootfs_dev(self) -> str:
        return self.current_rootfs_dev

    def get_current_boot_dev(self) -> str:
        return self.current_boot_dev

    def get_standby_rootfs_dev(self) -> str:
        return self.standby_rootfs_dev

    def get_standby_slot(self) -> str:
        return self.standby_slot

    def get_standby_boot_dev(self) -> str:
        return self.standby_boot_dev

    def is_external_rootfs_enabled(self) -> bool:
        return self.is_rootfs_on_external

    def mark_current_slot_boot_successful(self):
        slot = self.current_slot
        Nvbootctrl.mark_boot_successful(slot)

    def set_standby_slot_unbootable(self):
        slot = self.standby_slot
        Nvbootctrl.set_slot_as_unbootable(slot)

    def switch_boot(self):
        slot = self.standby_slot

        logger.info(f"switch boot to {slot=}")
        Nvbootctrl.set_active_boot_slot(slot)

    def is_current_slot_bootable(self) -> bool:
        slot = self.current_slot
        return Nvbootctrl.is_slot_bootable(slot)

    def is_current_slot_marked_successful(self) -> bool:
        slot = self.current_slot
        return Nvbootctrl.is_slot_marked_successful(slot)

    @classmethod
    def reboot(cls):
        try:
            subprocess_call("reboot", raise_exception=True)
        except CalledProcessError:
            logger.exception("failed to reboot")

    def update_extlinux_cfg(self, dst: Path, ref: Path):
        def _replace(ma: re.Match, repl: str):
            append_l: str = ma.group(0)
            if append_l.startswith("#"):
                return append_l
            res, n = re.compile(r"root=[\w\-=]*").subn(repl, append_l)
            if not n:
                res = f"{append_l} {repl}"

            return res

        _repl_func = partial(_replace, repl=f"root={self.standby_slot_partuuid}")
        dst.write_text(re.compile(r"\n\s*APPEND.*").sub(_repl_func, ref.read_text()))


class CBootController(
    PrepareMountMixin,
    SlotInUseMixin,
    OTAStatusMixin,
    VersionControlMixin,
    BootControllerProtocol,
):
    EXTLINUX_FILE = "/boot/extlinux/extlinux.conf"

    def __init__(self) -> None:
        self._cboot_control: _CBootControl = _CBootControl()

        # load paths
        ## first try to unmount standby dev if possible
        self.standby_slot_dev = self._cboot_control.get_standby_rootfs_dev()
        CMDHelperFuncs.umount(self.standby_slot_dev)

        self.standby_slot_mount_point = Path(cfg.MOUNT_POINT)
        self.standby_slot_mount_point.mkdir(exist_ok=True)

        ## refroot mount point
        _refroot_mount_point = cfg.REF_ROOT_MOUNT_POINT
        # first try to umount refroot mount point
        CMDHelperFuncs.umount(_refroot_mount_point)
        if not os.path.isdir(_refroot_mount_point):
            os.mkdir(_refroot_mount_point)
        self.ref_slot_mount_point = Path(_refroot_mount_point)

        ## ota-status dir
        ### current slot
        self.current_ota_status_dir = Path(cfg.OTA_STATUS_DIR)
        self.current_ota_status_dir.mkdir(parents=True, exist_ok=True)

        ## standby slot
        ### NOTE: not yet available before ota update starts
        self.standby_ota_status_dir = self.standby_slot_mount_point / Path(
            cfg.OTA_STATUS_DIR
        ).relative_to("/")

        # init ota-status
        self.ota_status = self._init_boot_control()

    ###### private methods ######

    def _init_boot_control(self) -> OTAStatusEnum:
        """Init boot control and ota-status on start-up."""
        _ota_status = self._load_current_ota_status()

        if _ota_status == OTAStatusEnum.UPDATING:
            _ota_status = self._finalize_update()
        elif _ota_status == OTAStatusEnum.ROLLBACKING:
            _ota_status = self._finalize_rollback()
        elif _ota_status == OTAStatusEnum.SUCCESS:
            # need to check whether it is negative SUCCESS
            current_slot = Nvbootctrl.get_current_slot()
            try:
                slot_in_use = self._load_current_slot_in_use()
                # cover the case that device reboot into unexpected slot
                if current_slot != slot_in_use:
                    logger.error(
                        f"boot into old slot {current_slot}, "
                        f"should boot into {slot_in_use}"
                    )
                    _ota_status = OTAStatusEnum.FAILURE

            except FileNotFoundError:
                # init slot_in_use file and ota_status file
                self._store_current_slot_in_use(current_slot)
                _ota_status = OTAStatusEnum.INITIALIZED

        # FAILURE, INITIALIZED and ROLLBACK_FAILURE are remained as it

        # NOTE: only update the current ota_status at ota-client launching up!
        self._store_current_ota_status(_ota_status)
        logger.info(f"loaded ota_status: {_ota_status}")
        return _ota_status

    def _is_switching_boot(self) -> bool:
        # evidence 1: nvbootctrl status
        # the newly updated slot should not be marked as successful on the first reboot
        _nvboot_res = not self._cboot_control.is_current_slot_marked_successful()

        # evidence 2: ota_status
        # the newly updated/rollbacked slot should have ota-status as updating/rollback
        _ota_status = self._load_current_ota_status() in [
            OTAStatusEnum.UPDATING,
            OTAStatusEnum.ROLLBACKING,
        ]

        # evidence 3: slot in use
        # the slot_in_use file should have the same slot as current slot
        _is_slot_in_use = (
            self._load_current_slot_in_use() == self._cboot_control.get_current_slot()
        )

        logger.info(
            f"[switch_boot detect result] nvboot: {_nvboot_res}, "
            f"ota_status: {_ota_status}, "
            f"slot_in_use: {_is_slot_in_use}"
        )
        return _nvboot_res and _ota_status and _is_slot_in_use

    def _finalize_update(self) -> OTAStatusEnum:
        logger.debug("entering finalizing stage...")
        if self._is_switching_boot():
            logger.debug("changes applied succeeded")
            # set the current slot(switched slot) as boot successful
            self._cboot_control.mark_current_slot_boot_successful()
            return OTAStatusEnum.SUCCESS
        else:
            logger.warning("changes applied failed")
            return OTAStatusEnum.FAILURE

    _finalize_rollback = _finalize_update

    def _populate_boot_folder_to_separate_bootdev(self):
        # mount the actual standby_boot_dev now
        _boot_dir_mount_point = Path(cfg.SEPARATE_BOOT_MOUNT_POINT)
        _boot_dir_mount_point.mkdir(exist_ok=True, parents=True)

        try:
            CMDHelperFuncs.mount_rw(
                self._cboot_control.get_standby_boot_dev(),
                _boot_dir_mount_point,
            )

            dst = _boot_dir_mount_point / "boot"
            dst.mkdir(exist_ok=True, parents=True)
            src = self.standby_slot_mount_point / "boot"

            # copy the standby slot's boot folder to emmc boot dev
            copytree_identical(src, dst)
        finally:
            try:
                # finish populating new boot folder to boot dev,
                # we can umount the boot dev right now
                CMDHelperFuncs.umount(_boot_dir_mount_point)
            except MountError as e:
                _failure_msg = f"failed to umount boot dev: {e!r}"
                logger.error(_failure_msg)
                # no need to raise to the caller

    def _unmount_all(self, *, ignore_error=False):
        """Umount standby and refroot."""

        # ignore errors on umounting
        CMDHelperFuncs.umount(self.standby_slot_mount_point, ignore_error=ignore_error)
        CMDHelperFuncs.umount(self.ref_slot_mount_point, ignore_error=ignore_error)

    def _on_operation_failure(self):
        """Failure registering and cleanup at failure."""
        self._store_standby_ota_status(OTAStatusEnum.FAILURE)
        logger.warning("on failure try to unmounting standby slot...")
        self._unmount_all(ignore_error=True)

    ###### public methods ######
    # also includes methods from OTAStatusMixin, VersionControlMixin

    def get_standby_slot_path(self) -> Path:
        return self.standby_slot_mount_point

    def get_standby_boot_dir(self) -> Path:
        """
        NOTE: in cboot controller, we directly use the /boot dir under the standby slot,
        and sync to the external boot dev in the post_update if needed.
        """
        return self.standby_slot_mount_point / "boot"

    def pre_update(self, version: str, *, standby_as_ref: bool, erase_standby=False):
        try:
            # setup updating
            self._cboot_control.set_standby_slot_unbootable()
            self._prepare_and_mount_standby(
                self._cboot_control.get_standby_rootfs_dev(),
                erase=erase_standby,
            )
            self._mount_refroot(
                standby_dev=self._cboot_control.get_standby_rootfs_dev(),
                active_dev=self._cboot_control.get_current_rootfs_dev(),
                standby_as_ref=standby_as_ref,
            )
            # create the ota-status folder unconditionally
            _ota_status_dir = self.standby_slot_mount_point / Path(
                cfg.OTA_STATUS_DIR
            ).relative_to("/")
            _ota_status_dir.mkdir(exist_ok=True, parents=True)

            # store status to standby slot
            self._store_standby_ota_status(OTAStatusEnum.UPDATING)
            self._store_standby_version(version)

            _target_slot = self._cboot_control.get_standby_slot()
            self._store_current_slot_in_use(_target_slot)
            self._store_standby_slot_in_use(_target_slot)

            logger.info("pre-update setting finished")

        except _BootControlError as e:
            logger.error(f"failed on pre_update: {e!r}")
            self._on_operation_failure()
            raise BootControlPreUpdateFailed from e

    def post_update(self):
        # TODO: deal with unexpected reboot during post_update
        try:
            # update extlinux_cfg file
            _extlinux_cfg = self.standby_slot_mount_point / Path(
                self.EXTLINUX_FILE
            ).relative_to("/")
            self._cboot_control.update_extlinux_cfg(
                dst=_extlinux_cfg, ref=_extlinux_cfg
            )

            if self._cboot_control.is_external_rootfs_enabled():
                logger.info(
                    "rootfs on external storage detected: "
                    "updating the /boot folder in standby bootdev..."
                )
                self._populate_boot_folder_to_separate_bootdev()

            self._cboot_control.switch_boot()

            logger.info("post update finished, rebooting...")
            self._unmount_all(ignore_error=True)
            self._cboot_control.reboot()

        except _BootControlError as e:
            logger.error(f"failed on post_update: {e!r}")
            self._on_operation_failure()
            raise BootControlPostUpdateFailed from e

    def post_rollback(self):
        try:
            self._cboot_control.switch_boot()
            self._cboot_control.reboot()
        except _BootControlError as e:
            logger.error(f"failed on post_rollback: {e!r}")
            self._on_operation_failure()
            raise BootControlPostRollbackFailed from e
