import os
import re
import shutil
from dataclasses import dataclass
from subprocess import CalledProcessError
from typing import ClassVar, Dict, List, Optional, Tuple
from pathlib import Path
from pprint import pformat

from app.boot_control.common import (
    ABPartitionError,
    CMDHelperFuncs,
    OTAStatusMixin,
    PrepareMountMixin,
    SlotInUseMixin,
    VersionControlMixin,
    cat_proc_cmdline,
)
from app.boot_control.interface import BootControllerProtocol
from app.common import (
    re_symlink_atomic,
    read_str_from_file,
    subprocess_check_output,
    write_str_to_file_sync,
)
from app.configs import BOOT_LOADER, grub_cfg as cfg
from app.errors import (
    BootControlInitError,
    BootControlPostRollbackFailed,
    BootControlPostUpdateFailed,
    BootControlPreRollbackFailed,
    BootControlPreUpdateFailed,
)
from app.proto import wrapper
from app import log_util

assert (
    BOOT_LOADER == "grub"
), f"ERROR, use grub instead of detected {BOOT_LOADER=}, abort"

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


@dataclass
class GrubMenuEntry:
    """
    NOTE: should only be called by the get_entry method

    linux: vmlinuz-<ver>
    linux_ver: <ver>
    initrd: initrd.img-<ver>
    """

    linux_ver: str
    title: str
    menuentry: str
    linux: str
    initrd: str
    rootfs_str: str

    def __init__(self, ma: re.Match) -> None:
        """
        NOTE: check GrubHelper for capturing group definition
        """
        self.linux_ver = ma.group("kernel_ver")
        self.title = ma.group("title")
        self.menuentry = ma.group("menu_entry")

        # get linux and initrd from menuentry
        if linux_ma := GrubHelper.linux_pa.search(self.menuentry):
            self.linux = linux_ma.group("kernel")
            self.rootfs_uuid_str = linux_ma.group("rootfs_str")
        if initrd_ma := GrubHelper.initrd_pa.search(self.menuentry):
            self.initrd = initrd_ma.group("initrd")

        assert self.linux and self.initrd, "failed to detect linux img and initrd"
        assert self.rootfs_uuid_str, "failed to detect rootfs_str"


class GrubHelper:
    menuentry_pa: ClassVar[re.Pattern] = re.compile(
        # whole capture group
        r"^(?P<menu_entry>\s*menuentry\s+"
        r"[^\{]*"  # menuentry options
        r"\{(?P<entry>[^\}]*)\}"  # menuentry block
        r")",  # end of whole capture
        re.MULTILINE | re.DOTALL,
    )
    linux_pa: ClassVar[re.Pattern] = re.compile(
        r"(?P<load_linux>^\s+linux\s+(?P<kernel_path>.*vmlinuz-(?P<ver>[\.\w\-]*)))"
        r"\s+(?P<cmdline>.*(?P<rootfs>root=(?P<rootfs_str>[\w\-=]*)).*)\s*$",
        re.MULTILINE,
    )
    rootfs_pa: ClassVar[re.Pattern] = re.compile(
        r"(?P<rootfs>root=(?P<rootfs_str>[\w\-=]*))"
    )

    initrd_pa: ClassVar[re.Pattern] = re.compile(
        r"^\s+initrd.*(?P<initrd>initrd.img-(?P<ver>[\.\w-]*))", re.MULTILINE
    )

    VMLINUZ = "vmlinuz"
    INITRD = "initrd.img"
    SUFFIX_OTA = "ota"
    SUFFIX_OTA_STANDBY = "ota.standby"
    KERNEL_OTA = f"{VMLINUZ}-{SUFFIX_OTA}"
    KERNEL_OTA_STANDBY = f"{VMLINUZ}-{SUFFIX_OTA_STANDBY}"
    INITRD_OTA = f"{INITRD}-{SUFFIX_OTA}"
    INITRD_OTA_STANDBY = f"{INITRD}-{SUFFIX_OTA_STANDBY}"

    grub_default_options: ClassVar[Dict[str, str]] = {
        "GRUB_TIMEOUT_STYLE": "menu",
        "GRUB_TIMEOUT": "10",
        "GRUB_DISABLE_SUBMENU": "y",
        "GRUB_DISABLE_OS_PROBER": "true",
        "GRUB_DISABLE_RECOVERY": "true",
    }

    @classmethod
    def update_entry_rootfs(
        cls, grub_cfg: str, *, kernel_ver: str, rootfs_str: str
    ) -> Optional[str]:
        """Read in grub_cfg, update all entries' rootfs with <rootfs_str>,
            and then return the updated one.

        Params:
            grub_cfg: input grub_cfg str
            kernel_ver: kernel version str for the target entry
            rootfs_str: a str that indicates which rootfs device to use
        """
        new_entry_block: Optional[str] = None
        entry_l, entry_r = None, None

        # loop over normal entry, find the target entry,
        # and then replace the rootfs string
        for entry in cls.menuentry_pa.finditer(grub_cfg):
            entry_l, entry_r = entry.span()
            entry_block = entry.group()
            # parse the entry block
            if _linux := cls.linux_pa.search(entry_block):
                if _linux.group("ver") == kernel_ver:
                    linux_line_l, linux_line_r = _linux.span()
                    _linux_line = _linux.group()
                    # replace rootfs string
                    new_entry_block = "%s%s%s" % (
                        entry_block[:linux_line_l],
                        cls.rootfs_pa.sub(rootfs_str, _linux_line),
                        entry_block[linux_line_r:],
                    )

        if new_entry_block is not None:
            return f"{grub_cfg[:entry_l]}{new_entry_block}{grub_cfg[entry_r:]}"

    @classmethod
    def get_entry(cls, grub_cfg: str, *, kernel_ver: str) -> Tuple[int, GrubMenuEntry]:
        for index, entry_ma in enumerate(cls.menuentry_pa.finditer(grub_cfg)):
            if _linux := cls.linux_pa.search(entry_ma.group()):
                if kernel_ver == _linux.group("ver"):
                    return index, GrubMenuEntry(entry_ma)

        raise ValueError(f"requested entry for {kernel_ver} not found")

    @classmethod
    def update_grub_default(
        cls, grub_default: str, *, default_entry_idx: Optional[int] = None
    ) -> str:
        """Read in grub_default str and return updated one."""
        kvp = cls.grub_default_options.copy()
        if default_entry_idx:
            kvp["GRUB_DEFAULT"] = f"{default_entry_idx}"

        res: List[str] = []
        for option_line in grub_default.splitlines():
            # NOTE: preserved empty or commented lines
            if not option_line or option_line.startswith("#"):
                res.append(option_line)
                continue

            key, _ = option_line.strip().split("=")
            if key in kvp:
                option_line = "=".join((key, kvp[key]))
                del kvp[key]

            res.append(option_line)

        # append options that haven't show up in the input
        for k, v in kvp.items():
            res.append("=".join((k, v)))

        return "\n".join(res)

    @classmethod
    def grub_mkconfig(cls) -> str:
        try:
            return subprocess_check_output("grub-mkconfig", raise_exception=True)
        except CalledProcessError as e:
            raise ValueError(
                f"grub-mkconfig failed: {e.returncode=}, {e.stderr=}, {e.stdout=}"
            )


class GrubABPartitionDetecter:
    """
    Expected layout:
    (system boots with legacy BIOS)
        /dev/sdx
            - sdx1: dedicated boot partition
            - sdx2: A partition
            - sdx3: B partition

    or
    (system boots with UEFI)
        /dev/sdx
            - sdx1: /boot/uefi
            - sdx2: /boot
            - sdx3: A partition
            - sdx4: B partition

    slot_name is the dev name of the A/B partition.
    We assume that last 2 partitions are A/B partitions, error will be raised
    if the current rootfs is not one of the last 2 partitions.
    """

    def __init__(self) -> None:
        self.active_slot, self.active_dev = self._detect_active_slot()
        self.standby_slot, self.standby_dev = self._detect_standby_slot(self.active_dev)

    def _get_sibling_dev(self, active_dev: str) -> str:
        """
        NOTE: revert to use previous detection mechanism.
        TODO: refine this method.
        """
        parent = CMDHelperFuncs.get_parent_dev(active_dev)
        boot_dev = CMDHelperFuncs.get_dev_by_mount_point("/boot")
        if not boot_dev:
            raise ABPartitionError("/boot is not mounted")

        # list children device file from parent device
        cmd = f"-Pp -o NAME,FSTYPE {parent}"
        # exclude parent dev
        output = CMDHelperFuncs._lsblk(cmd).splitlines()[1:]
        # FSTYPE="ext4" and
        # not (parent_device_file, root_device_file and boot_device_file)
        for blk in output:
            if m := re.search(r'NAME="(.*)"\s+FSTYPE="(.*)"', blk):
                if (
                    m.group(1) != active_dev
                    and m.group(1) != boot_dev
                    and m.group(2) == "ext4"
                ):
                    return m.group(1)

        raise ABPartitionError(f"{parent=} has unexpected partition layout: {output=}")

    def _detect_active_slot(self) -> Tuple[str, str]:
        """
        Returns:
            A tuple contains the slot_name and the full dev path
            of the active slot.
        """
        dev_path = CMDHelperFuncs.get_current_rootfs_dev()
        slot_name = dev_path.lstrip("/dev/")
        return slot_name, dev_path

    def _detect_standby_slot(self, active_dev: str) -> Tuple[str, str]:
        """
        Returns:
            A tuple contains the slot_name and the full dev path
            of the standby slot.
        """
        dev_path = self._get_sibling_dev(active_dev)
        slot_name = dev_path.lstrip("/dev/")
        return slot_name, dev_path

    ###### public methods ######
    def get_standby_slot(self) -> str:
        return self.standby_slot

    def get_standby_slot_dev(self) -> str:
        return self.standby_dev

    def get_active_slot(self) -> str:
        return self.active_slot

    def get_active_slot_dev(self) -> str:
        return self.active_dev


class _SymlinkABPartitionDetecter:
    """Implementation of legacy way to detect active/standby slot.

    NOTE: this is re-introduced for backward compatibility reason.

    Get the active slot by reading the symlink target of /boot/ota-partition.
    if ota-partition -> ota-partition.sda3, then active slot is sda3.

    If there are ota-partition.sda2 and ota-partition.sda3 exist under /boot, and
    ota-partition -> ota-partition.sda3, then sda2 is the standby slot.
    """

    @classmethod
    def _get_active_slot_by_symlink(cls) -> str:
        try:
            ota_partition_symlink = Path(cfg.BOOT_DIR) / cfg.BOOT_OTA_PARTITION_FILE
            active_ota_partition_file = os.readlink(ota_partition_symlink)

            return Path(active_ota_partition_file).suffix.strip(".")
        except FileNotFoundError:
            raise ABPartitionError("ota-partition files are broken")

    @classmethod
    def _get_standby_slot_by_symlink(cls) -> str:
        """
        NOTE: expecting to have only 2 ota-partition files for A/B partition each.
        """
        boot_dir = Path(cfg.BOOT_DIR)
        try:
            ota_partition_fs = list(boot_dir.glob(f"{cfg.BOOT_OTA_PARTITION_FILE}.*"))

            active_slot = cls._get_active_slot_by_symlink()
            active_slot_ota_partition_file = (
                boot_dir / f"{cfg.BOOT_OTA_PARTITION_FILE}.{active_slot}"
            )
            ota_partition_fs.remove(active_slot_ota_partition_file)

            assert len(ota_partition_fs) == 1
        except (ValueError, AssertionError):
            raise ABPartitionError("ota-partition files are broken")

        (standby_ota_partition_file,) = ota_partition_fs
        return standby_ota_partition_file.suffix.strip(".")


class _GrubControl:
    """Implementation of ota-partition switch boot mechanism."""

    def __init__(self) -> None:
        """NOTE: init only, no changes will be made in the __init__."""
        ab_detecter = GrubABPartitionDetecter()
        self.active_root_dev = ab_detecter.get_active_slot_dev()
        self.standby_root_dev = ab_detecter.get_standby_slot_dev()
        self.active_slot = ab_detecter.get_active_slot()
        self.standby_slot = ab_detecter.get_standby_slot()
        logger.info(f"{self.active_slot=}, {self.standby_slot=}")

        self.boot_dir = Path(cfg.BOOT_DIR)
        self.grub_file = Path(cfg.GRUB_CFG_PATH)
        self.grub_default_file = Path(cfg.DEFAULT_GRUB_PATH)

        self.ota_partition_folder = self.boot_dir / cfg.BOOT_OTA_PARTITION_FILE
        self.active_ota_partition_folder = (
            self.boot_dir / cfg.BOOT_OTA_PARTITION_FILE
        ).with_suffix(f".{self.active_slot}")
        self.active_grub_file = self.active_ota_partition_folder / "grub.cfg"

        self.standby_ota_partition_folder = (
            self.boot_dir / cfg.BOOT_OTA_PARTITION_FILE
        ).with_suffix(f".{self.standby_slot}")
        self.standby_grub_file = self.standby_ota_partition_folder / "grub.cfg"

        # create ota-partition folders for each
        self.active_ota_partition_folder.mkdir(exist_ok=True)
        self.standby_ota_partition_folder.mkdir(exist_ok=True)

    def _get_current_booted_kernel_and_initrd(self) -> Tuple[str, str]:
        """Return the name of booted kernel and initrd."""
        boot_cmdline = cat_proc_cmdline()
        if kernel_ma := re.search(
            r"BOOT_IMAGE=.*(?P<kernel>vmlinuz-(?P<ver>[\w\.\-]*))",
            boot_cmdline,
        ):
            kernel_ver = kernel_ma.group("ver")
        else:
            raise ValueError("failed to detect booted linux kernel")

        # lookup the grub file and find the booted entry
        _, entry = GrubHelper.get_entry(
            read_str_from_file(self.grub_file), kernel_ver=kernel_ver
        )
        logger.info(f"detected booted param: {entry.linux=}, {entry.initrd=}")
        return entry.linux, entry.initrd

    @staticmethod
    def _prepare_kernel_initrd_links_for_ota(target_folder: Path):
        """
        prepare links for kernel/initrd
        vmlinuz-ota -> vmlinuz-*
        initrd-ota -> initrd-*
        """
        kernel, initrd = None, None
        for f in target_folder.glob("*"):
            if (
                f.name.find(GrubHelper.VMLINUZ) == 0
                and not f.is_symlink()
                and kernel is None
            ):
                kernel = f.name
            elif (
                f.name.find(GrubHelper.INITRD) == 0
                and not f.is_symlink()
                and initrd is None
            ):
                initrd = f.name

            if kernel and initrd:
                break

        if not (kernel and initrd):
            raise ValueError(f"vmlinuz and/or initrd.img not found at {target_folder}")

        kernel_ota = target_folder / GrubHelper.KERNEL_OTA
        initrd_ota = target_folder / GrubHelper.INITRD_OTA
        re_symlink_atomic(kernel_ota, kernel)
        re_symlink_atomic(initrd_ota, initrd)
        logger.info(f"finished generate ota symlinks under {target_folder}")

    def _grub_update_for_active_slot(self, *, abort_on_standby_missed=True):
        """Generate current active grub_file from the view of current active slot.

        NOTE:
        1. this method only ensures the entry existence for ota(current active slot).
        2. this method ensures the default entry to be the current active slot.
        """
        # NOTE: If the path points to a symlink, exists() returns
        # whether the symlink points to an existing file or directory.
        active_vmlinuz = self.boot_dir / GrubHelper.KERNEL_OTA
        active_initrd = self.boot_dir / GrubHelper.INITRD_OTA
        if not (active_vmlinuz.exists() and active_initrd.exists()):
            msg = (
                "vmlinuz and/or initrd for active slot is not available, "
                "refuse to update_grub"
            )
            logger.error(msg)
            raise ValueError(msg)

        # step1: update grub_default file
        _in = self.grub_default_file.read_text()
        _out = GrubHelper.update_grub_default(_in)
        self.grub_default_file.write_text(_out)

        # step2: generate grub_cfg by grub-mkconfig
        # parse the output and find the active slot boot entry idx
        grub_cfg = GrubHelper.grub_mkconfig()
        if res := GrubHelper.get_entry(grub_cfg, kernel_ver=GrubHelper.SUFFIX_OTA):
            active_slot_entry_idx, _ = res
        else:
            raise ValueError("boot entry for ACTIVE slot not found, abort")

        # step3: update grub_default again, setting default to <idx>
        # ensure the active slot to be the default
        logger.info(
            f"boot entry for vmlinuz-ota(slot={self.active_slot}): {active_slot_entry_idx}"
        )
        _out = GrubHelper.update_grub_default(
            self.grub_default_file.read_text(),
            default_entry_idx=active_slot_entry_idx,
        )
        logger.debug(f"generated grub_default: {pformat(_out)}")
        write_str_to_file_sync(self.grub_default_file, _out)

        # step4: populate new active grub_file
        # update the ota.standby entry's rootfs uuid to standby slot's uuid
        grub_cfg = GrubHelper.grub_mkconfig()
        standby_uuid_str = CMDHelperFuncs.get_uuid_str_by_dev(self.standby_root_dev)
        if grub_cfg_updated := GrubHelper.update_entry_rootfs(
            grub_cfg,
            kernel_ver=GrubHelper.SUFFIX_OTA_STANDBY,
            rootfs_str=f"root={standby_uuid_str}",
        ):
            write_str_to_file_sync(self.active_grub_file, grub_cfg_updated)
            logger.info(f"standby rootfs: {standby_uuid_str}")
            logger.debug(f"generated grub_cfg: {pformat(grub_cfg_updated)}")
        else:
            msg = (
                "boot entry for standby slot not found, "
                "only current active slot's entry is populated."
            )
            if abort_on_standby_missed:
                raise ValueError(msg)

            logger.warning(msg)
            logger.info(f"generated grub_cfg: {pformat(grub_cfg)}")
            write_str_to_file_sync(self.active_grub_file, grub_cfg)

        # finally, symlink /boot/grub.cfg to ../ota-partition/grub.cfg
        ota_partition_folder = Path(cfg.BOOT_OTA_PARTITION_FILE)  # ota-partition
        re_symlink_atomic(  # /boot/grub/grub.cfg -> ../ota-partition/grub.cfg
            self.grub_file,
            Path("../") / ota_partition_folder / "grub.cfg",
        )
        logger.info(f"update_grub for {self.active_slot} finished.")

    def _ensure_ota_partition_symlinks(self):
        """
        NOTE: this method prepare symlinks from active slot's point of view.
        NOTE 2: grub_cfg symlink will not be generated here, it will be linked
                in grub_update method
        """
        # prepare ota-partition symlinks
        ota_partition_folder = Path(cfg.BOOT_OTA_PARTITION_FILE)  # ota-partition
        re_symlink_atomic(  # /boot/ota-partition -> ota-partition.<active_slot>
            self.boot_dir / ota_partition_folder,
            ota_partition_folder.with_suffix(f".{self.active_slot}"),
        )
        re_symlink_atomic(  # /boot/vmlinuz-ota -> ota-partition/vmlinuz-ota
            self.boot_dir / GrubHelper.KERNEL_OTA,
            ota_partition_folder / GrubHelper.KERNEL_OTA,
        )
        re_symlink_atomic(  # /boot/initrd.img-ota -> ota-partition/initrd.img-ota
            self.boot_dir / GrubHelper.INITRD_OTA,
            ota_partition_folder / GrubHelper.INITRD_OTA,
        )
        re_symlink_atomic(  # /boot/vmlinuz-ota.standby -> ota-partition.<standby_slot>/vmlinuz
            self.boot_dir / GrubHelper.KERNEL_OTA_STANDBY,
            ota_partition_folder.with_suffix(f".{self.standby_slot}")
            / GrubHelper.KERNEL_OTA,
        )
        re_symlink_atomic(  # /boot/initrd.img-ota.standby -> ota-partition.<standby_slot>/initrd.img-ota
            self.boot_dir / GrubHelper.INITRD_OTA_STANDBY,
            ota_partition_folder.with_suffix(f".{self.standby_slot}")
            / GrubHelper.INITRD_OTA,
        )

    ###### public methods ######
    def reprepare_active_ota_partition_file(self, *, abort_on_standby_missed: bool):
        self._prepare_kernel_initrd_links_for_ota(self.active_ota_partition_folder)
        # switch ota-partition symlink to current active slot
        self._ensure_ota_partition_symlinks()
        self._grub_update_for_active_slot(
            abort_on_standby_missed=abort_on_standby_missed
        )

    def reprepare_standby_ota_partition_file(self):
        """NOTE: this method still updates active grub file under active ota-partition folder."""
        self._prepare_kernel_initrd_links_for_ota(self.standby_ota_partition_folder)
        self._ensure_ota_partition_symlinks()
        self._grub_update_for_active_slot(abort_on_standby_missed=True)

    def init_active_ota_partition_file(self):
        """Prepare active ota-partition folder and ensure the existence of
        symlinks needed for ota update.

        GrubController supports migrates system that doesn't boot via ota-partition
        mechanism(possibly using different grub configuration, i.e., grub submenu enabled)
        to use ota-partition.

        NOTE:
        1. only update the ota-partition.<active_slot>/grub.cfg!
        2. standby slot is not considered here!
        3. expected previously booted kernel/initrd to be located at /boot
        """
        # check the current booted kernel,
        # if it is not vmlinuz-ota, copy that kernel to active ota_partition folder
        cur_kernel, cur_initrd = self._get_current_booted_kernel_and_initrd()
        if cur_kernel != GrubHelper.KERNEL_OTA or cur_initrd != GrubHelper.INITRD_OTA:
            logger.info(
                "system doesn't use ota-partition mechanism to boot, "
                "initializing ota-partition file..."
            )
            # NOTE: just copy but not cleanup the existed kernel/initrd files
            shutil.copy(
                self.boot_dir / cur_kernel,
                self.active_ota_partition_folder,
                follow_symlinks=True,
            )
            shutil.copy(
                self.boot_dir / cur_initrd,
                self.active_ota_partition_folder,
                follow_symlinks=True,
            )
            self.reprepare_active_ota_partition_file(abort_on_standby_missed=False)

        logger.info("ota-partition file initialized")

    def grub_reboot_to_standby(self):
        self.reprepare_standby_ota_partition_file()
        idx, _ = GrubHelper.get_entry(
            read_str_from_file(self.grub_file),
            kernel_ver=GrubHelper.SUFFIX_OTA_STANDBY,
        )
        CMDHelperFuncs.grub_reboot(idx)
        logger.info(f"system will reboot to {self.standby_slot=}: boot entry {idx}")

    finalize_update_switch_boot = reprepare_active_ota_partition_file


class GrubController(
    VersionControlMixin,
    OTAStatusMixin,
    PrepareMountMixin,
    SlotInUseMixin,
    BootControllerProtocol,
):
    def __init__(self) -> None:
        try:
            self._boot_control = _GrubControl()

            # try to unmount standby dev if possible
            CMDHelperFuncs.umount(self._boot_control.standby_root_dev)
            self.standby_slot_mount_point = Path(cfg.MOUNT_POINT)
            self.standby_slot_mount_point.mkdir(exist_ok=True)

            ## ota-status dir
            self.current_ota_status_dir = self._boot_control.active_ota_partition_folder
            self.standby_ota_status_dir = (
                self._boot_control.standby_ota_partition_folder
            )

            # refroot mount point
            self.ref_slot_mount_point = Path(cfg.REF_ROOT_MOUNT_POINT)
            # try to umount refroot mount point
            CMDHelperFuncs.umount(self.ref_slot_mount_point)
            if not os.path.isdir(self.ref_slot_mount_point):
                os.mkdir(self.ref_slot_mount_point)

            # init boot control
            #   1. load/process ota_status
            #   2. finalize update/rollback or init boot files
            self._init_boot_control()
        except Exception as e:
            logger.error(f"failed on init boot controller: {e!r}")
            raise BootControlInitError from e

    def _init_boot_control(self):
        # load ota_status str and slot_in_use
        _ota_status = self._load_current_ota_status()
        _slot_in_use = self._load_current_slot_in_use()

        # NOTE: for backward compatibility, only check otastatus file
        if not _ota_status:
            logger.info("initializing boot control files...")
            _ota_status = wrapper.StatusOta.INITIALIZED
            self._boot_control.init_active_ota_partition_file()
            self._store_current_slot_in_use(self._boot_control.active_slot)
            self._store_current_ota_status(wrapper.StatusOta.INITIALIZED)

        # populate slot_in_use file if it doesn't exist
        if not _slot_in_use:
            self._store_current_slot_in_use(self._boot_control.active_slot)

        if _ota_status in [wrapper.StatusOta.UPDATING, wrapper.StatusOta.ROLLBACKING]:
            if self._is_switching_boot():
                self._boot_control.finalize_update_switch_boot(
                    abort_on_standby_missed=True
                )
                # switch ota_status
                _ota_status = wrapper.StatusOta.SUCCESS
            else:
                if _ota_status == wrapper.StatusOta.ROLLBACKING:
                    _ota_status = wrapper.StatusOta.ROLLBACK_FAILURE
                else:
                    _ota_status = wrapper.StatusOta.FAILURE
        # other ota_status will remain the same

        # detect failed reboot, but only print error logging
        if (
            _ota_status != wrapper.StatusOta.INITIALIZED
            and _slot_in_use
            and _slot_in_use != self._boot_control.active_slot
        ):
            logger.error(
                f"boot into old slot {self._boot_control.active_slot}, "
                f"but slot_in_use indicates it should boot into {_slot_in_use}, "
                "this might indicate a failed finalization at first reboot after update/rollback"
            )

        # apply ota_status to otaclient
        self.ota_status = _ota_status
        self._store_current_ota_status(_ota_status)
        logger.info(f"boot control init finished, ota_status is {_ota_status}")

    def _is_switching_boot(self):
        # evidence 1: ota_status should be updating/rollbacking at the first reboot
        _check_ota_status = self._load_current_ota_status() in [
            wrapper.StatusOta.UPDATING,
            wrapper.StatusOta.ROLLBACKING,
        ]

        # NOTE(20220714): maintain backward compatibility, not using slot_in_use
        # file here to detect switching boot. Maybe enable it in the future.

        # evidence 2(legacy): ota-partition.standby should be the
        # current booted slot, because ota-partition symlink is not yet switched
        # at the first reboot.
        _target_slot = _SymlinkABPartitionDetecter._get_standby_slot_by_symlink()
        _check_slot_in_use = _target_slot == self._boot_control.active_slot

        # evidence 2: slot_in_use file should have the same slot as current slot
        # _target_slot = self._load_current_slot_in_use()
        # _check_slot_in_use = _target_slot == self._boot_control.active_slot

        res = _check_ota_status and _check_slot_in_use
        logger.info(
            f"_is_switching_boot: {res} "
            f"({_check_ota_status=}, {_check_slot_in_use=})"
        )
        return res

    def _finalize_update(self) -> wrapper.StatusOta:
        if self._is_switching_boot():
            self._boot_control.finalize_update_switch_boot(abort_on_standby_missed=True)
            return wrapper.StatusOta.SUCCESS
        else:
            return wrapper.StatusOta.FAILURE

    _finalize_rollback = _finalize_update

    def _update_fstab(self, *, active_slot_fstab: Path, standby_slot_fstab: Path):
        """Update standby fstab based on active slot's fstab and just installed new stanby fstab.

        Override existed entries in standby fstab, merge new entries from active fstab.
        """
        standby_uuid_str = CMDHelperFuncs.get_uuid_str_by_dev(
            self._boot_control.standby_root_dev
        )
        fstab_entry_pa = re.compile(
            r"^\s*(?P<file_system>[^# ]*)\s+"
            r"(?P<mount_point>[^ ]*)\s+"
            r"(?P<type>[^ ]*)\s+"
            r"(?P<options>[^ ]*)\s+"
            r"(?P<dump>[\d]*)\s+(?P<pass>[\d]*)",
            re.MULTILINE,
        )

        # standby partition fstab (to be merged)
        fstab_standby = read_str_from_file(standby_slot_fstab, missing_ok=False)
        fstab_standby_dict: Dict[str, re.Match] = {}
        for line in fstab_standby.splitlines():
            if ma := fstab_entry_pa.match(line):
                if ma.group("mount_point") == "/":
                    continue
                fstab_standby_dict[ma.group("mount_point")] = ma

        # merge entries
        merged: List[str] = []
        fstab_active = read_str_from_file(active_slot_fstab, missing_ok=False)
        for line in fstab_active.splitlines():
            if ma := fstab_entry_pa.match(line):
                mp = ma.group("mount_point")
                if mp == "/":  # rootfs mp, unconditionally replace uuid
                    _list = list(ma.groups())
                    _list[0] = standby_uuid_str
                    merged.append("\t".join(_list))
                elif mp in fstab_standby_dict:
                    merged.append("\t".join(fstab_standby_dict[mp].groups()))
                    del fstab_standby_dict[mp]
                else:
                    merged.append("\t".join(ma.groups()))
            else:
                # re-add comments to merged
                merged.append(line)

        # merge standby_fstab's left-over lines
        for _, ma in fstab_standby_dict.items():
            merged.append("\t".join(ma.groups()))

        # write to standby fstab
        write_str_to_file_sync(standby_slot_fstab, "\n".join(merged))

    def cleanup_standby_ota_partition_folder(self):
        """Cleanup old files under the standby ota-partition folder."""
        files_keept = (
            cfg.OTA_STATUS_FNAME,
            cfg.OTA_VERSION_FNAME,
            cfg.SLOT_IN_USE_FNAME,
            Path(cfg.GRUB_CFG_PATH).name,
        )
        removes = (
            f
            for f in self.standby_ota_status_dir.glob("*")
            if f.name not in files_keept
        )
        for f in removes:
            if f.is_dir():
                shutil.rmtree(f, ignore_errors=True)
            else:
                f.unlink(missing_ok=True)

    ###### public methods ######
    # also includes methods from OTAStatusMixin, VersionControlMixin
    # load_version, get_ota_status
    def on_operation_failure(self):
        """Failure registering and cleanup at failure."""
        self._store_standby_ota_status(wrapper.StatusOta.FAILURE)
        self._store_current_ota_status(wrapper.StatusOta.FAILURE)
        logger.warning("on failure try to unmounting standby slot...")
        self._umount_all(ignore_error=True)

    def get_standby_slot_path(self) -> Path:
        return self.standby_slot_mount_point

    def get_standby_boot_dir(self) -> Path:
        """
        NOTE: in grub_controller, kernel and initrd images are stored under
        the ota_status_dir(ota_partition_dir)
        """
        return self.standby_ota_status_dir

    def pre_update(self, version: str, *, standby_as_ref: bool, erase_standby=False):
        try:
            # update ota_status files
            self._store_current_ota_status(wrapper.StatusOta.FAILURE)
            self._store_standby_ota_status(wrapper.StatusOta.UPDATING)
            # update version file
            self._store_standby_version(version)
            # update slot_in_use file
            # set slot_in_use to <standby_slot> to both slots
            _target_slot = self._boot_control.standby_slot
            self._store_current_slot_in_use(_target_slot)
            self._store_standby_slot_in_use(_target_slot)

            # enter pre-update
            self._prepare_and_mount_standby(
                self._boot_control.standby_root_dev,
                erase=erase_standby,
            )
            self._mount_refroot(
                standby_dev=self._boot_control.standby_root_dev,
                active_dev=self._boot_control.active_root_dev,
                standby_as_ref=standby_as_ref,
            )
            # remove old files under standby ota_partition folder
            self.cleanup_standby_ota_partition_folder()
        except Exception as e:
            logger.error(f"failed on pre_update: {e!r}")
            raise BootControlPreUpdateFailed from e

    def post_update(self):
        try:
            # update fstab
            active_fstab = Path(cfg.FSTAB_FILE_PATH)
            standby_fstab = self.standby_slot_mount_point / active_fstab.relative_to(
                "/"
            )
            self._update_fstab(
                standby_slot_fstab=standby_fstab,
                active_slot_fstab=active_fstab,
            )
            # umount all mount points after local update finished
            self._umount_all(ignore_error=True)

            self._boot_control.grub_reboot_to_standby()
            CMDHelperFuncs.reboot()
        except Exception as e:
            logger.error(f"failed on post_update: {e!r}")
            raise BootControlPostUpdateFailed from e

    def pre_rollback(self):
        try:
            self._store_current_ota_status(wrapper.StatusOta.FAILURE)
            self._store_standby_ota_status(wrapper.StatusOta.ROLLBACKING)
        except Exception as e:
            logger.error(f"failed on pre_rollback: {e!r}")
            raise BootControlPreRollbackFailed from e

    def post_rollback(self):
        try:
            self._boot_control.grub_reboot_to_standby()
            CMDHelperFuncs.reboot()
        except Exception as e:
            logger.error(f"failed on pre_rollback: {e!r}")
            raise BootControlPostRollbackFailed from e
