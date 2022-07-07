import os
from pprint import pformat
import re
import shutil
from dataclasses import dataclass
from subprocess import CalledProcessError
from typing import ClassVar, Dict, List, Optional, Tuple
from pathlib import Path

from app.boot_control.common import (
    GrubABPartitionDetecter,
    CMDHelperFuncs,
    OTAStatusMixin,
    PrepareMountMixin,
    SlotInUseMixin,
    VersionControlMixin,
)
from app.boot_control.interface import BootControllerProtocol
from app.common import (
    re_symlink_atomic,
    read_from_file,
    subprocess_call,
    subprocess_check_output,
    write_to_file_sync,
)
from app.configs import BOOT_LOADER, grub_cfg as cfg
from app.errors import (
    BootControlInitError,
    BootControlPostRollbackFailed,
    BootControlPostUpdateFailed,
    BootControlPreUpdateFailed,
)
from app.ota_status import OTAStatusEnum
from app import log_util

assert BOOT_LOADER == "grub", f"ERROR, try to use grub on {BOOT_LOADER}, abort"

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
    rootfs_partuuid_str: str  # format: PARTUUID=xxx

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
            self.rootfs_partuuid_str = linux_ma.group("rootfs_partuuid_str")
        if initrd_ma := GrubHelper.initrd_pa.search(self.menuentry):
            self.initrd = initrd_ma.group("initrd")

        assert self.linux and self.initrd, "failed to detect linux img and initrd"
        assert self.rootfs_partuuid_str, "failed to detect rootfs_partuuid_str"


class GrubHelper:
    menuentry_pa: ClassVar[re.Pattern] = re.compile(
        # whole capture group
        r"^(?P<menu_entry>\s*menuentry\s+"
        # menuentry title:
        # format: <os>, with Linux <kernel_ver>[ [<recovery>] [<os_probe>]]
        r"'(?P<title>[^,]*, +with +Linux +(?P<kernel_ver>[^'\s]*)(?P<other>( *\([^\)]*\))*)?)'"
        r"[^\{]*"  # menuentry options
        r"\{(?P<entry>[^\}]*)\}"  # menuentry block
        r")",  # end of whole capture
        re.MULTILINE | re.DOTALL,
    )
    linux_pa: ClassVar[re.Pattern] = re.compile(
        r"(?P<left>^\s+linux.*(?P<kernel>vmlinuz-(?P<ver>[\.\w\-]*)) +.*)"
        r"(?P<rootfs>root=(?P<rootfs_partuuid_str>[\w\-=]*))"
        r"(?P<right>.*)",
        re.MULTILINE,
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
        "GRUB_DEFAULT": "'Ubuntu, with Linux ota'",
        "GRUB_DISABLE_RECOVERY": "true",
    }

    @classmethod
    def update_entry_rootfs(
        cls, grub_cfg: str, *, kernel_ver: str, rootfs_str: str
    ) -> Optional[str]:
        """Read in grub_cfg and return updated one.
        NOTE: we only update normal entry for the <kernel_ver>,
        recovery mode or other special entry will be ignored.

        Params:
            grub_cfg: input grub_cfg str
            kernel_ver: kernel version str for the target entry
            rootfs_str: a str that indicates which rootfs device to use,
                support partuuid(root=PARTUUID=xxx)
        """
        new_entry_block: Optional[str] = None
        entry_l, entry_r = None, None

        # loop over normal entry, find the target entry,
        # and then replace the rootfs string
        for entry in cls.menuentry_pa.finditer(grub_cfg):
            entry_l, entry_r = entry.span()
            entry_block = entry.group()
            # NOTE: ignore recovery entry
            if entry.group("kernel_ver") == kernel_ver and not entry.group("other"):
                if linux_line := cls.linux_pa.search(entry_block):
                    linux_line_l, linux_line_r = linux_line.span()
                    new_linux_line = "%s%s%s" % (
                        linux_line.group("left"),
                        rootfs_str,
                        linux_line.group("right"),
                    )
                    new_entry_block = (
                        f"{entry_block[:linux_line_l]}"
                        f"{new_linux_line}"
                        f"{entry_block[linux_line_r:]}"
                    )
                    break

        if new_entry_block is not None:
            return f"{grub_cfg[:entry_l]}{new_entry_block}{grub_cfg[entry_r:]}"

    @classmethod
    def get_entry(cls, grub_cfg: str, *, kernel_ver: str) -> Tuple[int, GrubMenuEntry]:
        for index, entry_ma in enumerate(cls.menuentry_pa.finditer(grub_cfg)):
            if kernel_ver == entry_ma.group("kernel_ver"):
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
        for l in grub_default.splitlines():
            # NOTE: preserved empty or commented lines
            if not l or l.startswith("#"):
                res.append(l)
                continue

            key, _ = l.strip().split("=")
            if key in kvp:
                l = "=".join((key, kvp[key]))
                del kvp[key]

            res.append(l)

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


class _GrubControl:
    def __init__(self) -> None:
        """NOTE: init only, no changes will be made in the __init__."""
        ab_detecter = GrubABPartitionDetecter()
        self.active_root_dev = ab_detecter.get_active_slot_dev()
        self.standby_root_dev = ab_detecter.get_standby_slot_dev()
        self.active_slot = ab_detecter.get_active_slot()
        self.standby_slot = ab_detecter.get_standby_slot()
        logger.info(f"{self.active_slot=}, {self.standby_slot=}")

        self.boot_dir = Path("/boot")
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

        self.standby_slot_partuuid_str = CMDHelperFuncs.get_partuuid_str_by_dev(
            self.standby_root_dev
        )

        self.active_ota_partition_folder.mkdir(exist_ok=True)
        self.standby_ota_partition_folder.mkdir(exist_ok=True)

    def _get_current_booted_kernel_and_initrd(self) -> Tuple[str, str]:
        """Return the name of booted kernel and initrd."""
        boot_cmdline = read_from_file("/proc/cmdline")
        if kernel_ma := re.search(
            boot_cmdline,
            r".*BOOT_IMAGE=[^ ]*(?P<kernel>vmlinuz-(?P<ver>[\w\.\-]*))",
        ):
            kernel_ver = kernel_ma.group("ver")
        else:
            raise ValueError("failed to detect booted linux kernel")

        # lookup the grub file and find the booted entry
        _, entry = GrubHelper.get_entry(
            read_from_file(self.grub_file), kernel_ver=kernel_ver
        )
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
            if not f.is_symlink() and f.name.find(GrubHelper.VMLINUZ) == 0:
                kernel = f.name
            elif not f.is_symlink() and f.name.find(GrubHelper.INITRD) == 0:
                initrd = f.name
            elif kernel and initrd:
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
        logger.info(f"boot entry for {self.active_slot=}: {active_slot_entry_idx}")
        _out = GrubHelper.update_grub_default(
            self.grub_default_file.read_text(),
            default_entry_idx=active_slot_entry_idx,
        )
        logger.debug(f"generated grub_default: {pformat(_out)}")
        write_to_file_sync(self.grub_default_file, _out)

        # step4: populate new active grub_file
        # update the ota.standby entry's rootfs uuid to standby slot's uuid
        grub_cfg = GrubHelper.grub_mkconfig()
        if grub_cfg_updated := GrubHelper.update_entry_rootfs(
            grub_cfg,
            kernel_ver=GrubHelper.SUFFIX_OTA_STANDBY,
            rootfs_str=f"root={self.standby_slot_partuuid_str}",
        ):
            write_to_file_sync(self.active_grub_file, grub_cfg_updated)
            logger.info(f"standby rootfs: {self.standby_slot_partuuid_str}")
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
            write_to_file_sync(self.active_grub_file, grub_cfg)

        logger.info(f"update_grub for {self.active_slot} finished.")

    def _ensure_ota_partition_symlinks(self):
        """
        NOTE: this method prepare symlinks from active slot's point of view.
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
        re_symlink_atomic(  # /boot/grub/grub.cfg -> ../ota-partition/grub.cfg
            self.grub_file,
            Path("../") / ota_partition_folder / "grub.cfg",
        )

    ###### public methods ######
    def reprepare_active_ota_partition_file(self, *, abort_on_standby_missed: bool):
        self._prepare_kernel_initrd_links_for_ota(self.active_ota_partition_folder)
        self._ensure_ota_partition_symlinks()
        self._grub_update_for_active_slot(
            abort_on_standby_missed=abort_on_standby_missed
        )

    def reprepare_standby_ota_partition_file(self):
        self._prepare_kernel_initrd_links_for_ota(self.standby_ota_partition_folder)
        self._ensure_ota_partition_symlinks()
        # NOTE: always update active slots' grub file
        self._grub_update_for_active_slot(abort_on_standby_missed=True)

    def init_active_ota_partition_file(self):
        """Prepare active ota-partition folder and ensure the existence of
        symlinks needed for ota update.

        NOTE:
        1. only update the ota-partition.<active_slot>/grub.cfg!
        2. standby slot is not considered here!
        """
        # check the current booted kernel,
        # if it is not vmlinuz-ota, copy that kernel to active ota_partition folder
        cur_kernel, cur_initrd = self._get_current_booted_kernel_and_initrd()
        if not (
            cur_kernel != GrubHelper.KERNEL_OTA and cur_initrd != GrubHelper.INITRD_OTA
        ):
            logger.info(
                "system doesn't use ota-partition mechanism to boot"
                "initializing ota-partition file..."
            )
            # NOTE: just copy but not cleanup the existed kernel/initrd files
            shutil.copy(
                cur_kernel, self.active_ota_partition_folder, follow_symlinks=True
            )
            shutil.copy(
                cur_initrd, self.active_ota_partition_folder, follow_symlinks=True
            )
            self.reprepare_active_ota_partition_file(abort_on_standby_missed=False)

        logger.info("ota-partition file initialized")

    def grub_reboot_to_standby(self):
        self.reprepare_standby_ota_partition_file()
        idx, _ = GrubHelper.get_entry(
            read_from_file(self.grub_file),
            kernel_ver=GrubHelper.SUFFIX_OTA_STANDBY,
        )
        subprocess_call(f"grub-reboot {idx}", raise_exception=True)
        logger.info(f"system will reboot to {self.standby_slot=}: boot entry {idx}")

    def finalize_update_switch_boot(self):
        self.reprepare_active_ota_partition_file(abort_on_standby_missed=True)


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
            _ota_status = self._load_current_ota_status()
            if _ota_status == OTAStatusEnum.UPDATING:
                _ota_status = self._finalize_update()
            elif _ota_status == OTAStatusEnum.ROLLBACKING:
                _ota_status = self._finalize_rollback()
            elif _ota_status == OTAStatusEnum.SUCCESS:
                # need to check whether it is negative SUCCESS
                current_slot = self._boot_control.active_slot
                try:
                    slot_in_use = self._load_current_slot_in_use()
                    # cover the case that device reboot into unexpected slot
                    if current_slot != slot_in_use:
                        logger.error(
                            f"boot into old slot {current_slot}, "
                            f"expect to boot into {slot_in_use}"
                        )
                        _ota_status = OTAStatusEnum.FAILURE

                except FileNotFoundError:
                    # init slot_in_use file and ota_status file
                    self._store_current_slot_in_use(current_slot)
                    _ota_status = OTAStatusEnum.INITIALIZED
            # INITIALIZED, FAILURE and ROLLBACK_FAILURE are remained as it

            # special treatment on initialized status
            if _ota_status == OTAStatusEnum.INITIALIZED:
                self._boot_control.init_active_ota_partition_file()

            # NOTE: only update the current ota_status at ota-client launching up!
            self.ota_status = _ota_status
            self._store_current_ota_status(_ota_status)
            logger.info(f"loaded ota_status: {_ota_status}")
        except Exception as e:
            # NOTE: store failure to current slot ONLY at boot control init failed!
            self._store_current_ota_status(OTAStatusEnum.FAILURE)
            raise BootControlInitError from e

    def _is_switching_boot(self):
        # evidence 1: ota_status should be updating/rollbacking at the first reboot
        _check_ota_status = self._load_current_ota_status() in [
            OTAStatusEnum.UPDATING,
            OTAStatusEnum.ROLLBACKING,
        ]

        # evidence 2: slot_in_use file should have the same slot as current slot
        _current_slot = self._load_current_slot_in_use()
        _check_slot_in_use = _current_slot == self._boot_control.active_slot

        res = _check_ota_status and _check_slot_in_use
        logger.info(
            f"_is_switching_boot: {res} "
            f"({_check_ota_status=}, {_check_slot_in_use=})"
        )
        return res

    def _finalize_update(self) -> OTAStatusEnum:
        if self._is_switching_boot():
            self._boot_control.finalize_update_switch_boot()
            return OTAStatusEnum.SUCCESS
        else:
            return OTAStatusEnum.FAILURE

    _finalize_rollback = _finalize_update

    def _update_fstab(self, *, active_slot_fstab: Path, standby_slot_fstab: Path):
        """Update standby fstab based on active slot's fstab and just installed new stanby fstab.

        Override existed entries in standby fstab, merge new entries from active fstab.
        """
        standby_partuuid_str = CMDHelperFuncs.get_partuuid_str_by_dev(
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
        fstab_standby = read_from_file(standby_slot_fstab, missing_ok=False)
        fstab_standby_dict: Dict[str, re.Match] = {}
        for line in fstab_standby.splitlines():
            if ma := fstab_entry_pa.match(line):
                if ma.group("mount_point") == "/":
                    continue
                fstab_standby_dict[ma.group("mount_point")] = ma

        # merge entries
        merged: List[str] = []
        fstab_active = read_from_file(active_slot_fstab, missing_ok=False)
        for line in fstab_active.splitlines():
            if ma := fstab_entry_pa.match(line):
                mp = ma.group("mount_point")
                if mp == "/":  # rootfs mp, unconditionally replace uuid
                    _list = list(ma.groups())
                    _list[0] = standby_partuuid_str
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
        write_to_file_sync(standby_slot_fstab, "\n".join(merged))

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

    def _on_operation_failure(self):
        """Failure registering and cleanup at failure."""
        self._store_standby_ota_status(OTAStatusEnum.FAILURE)
        logger.warning("on failure try to unmounting standby slot...")
        self._umount_all(ignore_error=True)

    ###### public methods ######
    # also includes methods from OTAStatusMixin, VersionControlMixin
    # load_version, get_ota_status

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

            self._store_standby_ota_status(OTAStatusEnum.UPDATING)
            self._store_standby_version(version)

            # set slot_in_use to <standby_slot> to both slots
            _target_slot = self._boot_control.standby_slot
            self._store_current_slot_in_use(_target_slot)
            self._store_standby_slot_in_use(_target_slot)
        except Exception as e:
            logger.error(f"failed on pre_update: {e!r}")
            self._on_operation_failure()
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
            subprocess_call("reboot")
        except Exception as e:
            logger.error(f"failed on post_update: {e!r}")
            self._on_operation_failure()
            raise BootControlPostUpdateFailed from e

    def post_rollback(self):
        try:
            self._boot_control.grub_reboot_to_standby()
            subprocess_call("reboot")
        except Exception as e:
            logger.error(f"failed on pre_rollback: {e!r}")
            self._on_operation_failure()
            raise BootControlPostRollbackFailed from e
