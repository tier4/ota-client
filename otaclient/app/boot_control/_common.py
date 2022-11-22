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
r"""Shared utils for boot_controller."""


from pathlib import Path
from subprocess import CalledProcessError
from typing import List, Optional, Union, Callable, Any

from ._errors import BootControlError, MountError, MkfsError, MountFailedReason

from .. import log_setting
from ..configs import config as cfg
from ..common import (
    read_str_from_file,
    subprocess_call,
    subprocess_check_output,
    write_str_to_file_sync,
)
from ..proto import wrapper


logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class CMDHelperFuncs:
    """HelperFuncs bundle for wrapped linux cmd."""

    @staticmethod
    @MountFailedReason.parse_failed_reason
    def _mount(
        dev: str,
        mount_point: str,
        *,
        options: Optional[List[str]] = None,
        args: Optional[List[str]] = None,
    ):
        """
        mount [-o option1[,option2, ...]]] [args[0] [args[1]...]] <dev> <mount_point>
        """
        _option_str = ""
        if options:
            _option_str = f"-o {','.join(options)}"

        _args_str = ""
        if args:
            _args_str = f"{' '.join(args)}"

        _cmd = f"mount {_option_str} {_args_str} {dev} {mount_point}"
        subprocess_call(_cmd, raise_exception=True)

    @staticmethod
    def _findfs(key: str, value: str) -> str:
        """
        findfs finds a partition by conditions
        Usage:
            findfs [options] {LABEL,UUID,PARTUUID,PARTLABEL}=<value>
        """
        _cmd = f"findfs {key}={value}"
        return subprocess_check_output(_cmd)

    @staticmethod
    def _findmnt(args: str) -> str:
        _cmd = f"findmnt {args}"
        return subprocess_check_output(_cmd)

    @staticmethod
    def _lsblk(args: str, *, raise_exception=False) -> str:
        _cmd = f"lsblk {args}"
        return subprocess_check_output(_cmd, raise_exception=raise_exception)

    ###### derived helper methods ######

    @classmethod
    def get_fslabel_by_dev(cls, dev: str) -> str:
        """Return partuuid of input device."""
        args = f"-in -o LABEL {dev}"
        try:
            return cls._lsblk(args, raise_exception=True)
        except ValueError as e:
            msg = f"failed to get fslabel for {dev}: {e}"
            raise ValueError(msg) from None

    @classmethod
    def get_partuuid_by_dev(cls, dev: str) -> str:
        """Return partuuid of input device."""
        args = f"-in -o PARTUUID {dev}"
        try:
            return cls._lsblk(args, raise_exception=True)
        except ValueError as e:
            msg = f"failed to get partuuid for {dev}: {e}"
            raise ValueError(msg) from None

    @classmethod
    def get_uuid_by_dev(cls, dev: str) -> str:
        """Return uuid of input device."""
        args = f"-in -o UUID {dev}"
        try:
            return cls._lsblk(args, raise_exception=True)
        except ValueError as e:
            msg = f"failed to get uuid for {dev}: {e}"
            raise ValueError(msg) from None

    @classmethod
    def get_uuid_str_by_dev(cls, dev: str) -> str:
        """Return UUID string of input device.

        Returns:
            str like: "UUID=<uuid>"
        """
        return f"UUID={cls.get_uuid_by_dev(dev)}"

    @classmethod
    def get_partuuid_str_by_dev(cls, dev: str) -> str:
        """Return PARTUUID string of input device.

        Returns:
            str like: "PARTUUID=<partuuid>"
        """
        return f"PARTUUID={cls.get_partuuid_by_dev(dev)}"

    @classmethod
    def get_dev_by_partlabel(cls, partlabel: str) -> str:
        return cls._findfs("PARTLABEL", partlabel)

    @classmethod
    def get_dev_by_fslabel(cls, fslabel: str) -> str:
        return cls._findfs("LABEL", fslabel)

    @classmethod
    def get_dev_by_partuuid(cls, partuuid: str) -> str:
        return cls._findfs("PARTUUID", partuuid)

    @classmethod
    def get_current_rootfs_dev(cls) -> str:
        """
        NOTE:
            -o <COLUMN>: only print <COLUMN>
            -n: no headings
            -f: only show the first file system
            -c: canonicalize printed paths

        Returns:
            full path to dev of the current rootfs
        """
        if res := cls._findmnt("/ -o SOURCE -n -f -c"):
            return res
        else:
            raise BootControlError("failed to detect current rootfs")

    @classmethod
    def get_mount_point_by_dev(cls, dev: str, *, raise_exception=True) -> str:
        """
        findmnt <dev> -o TARGET -n

        NOTE: findmnt raw result might have multiple lines if target dev is bind mounted.
        use option -f to only show the first file system
        """
        mount_points = cls._findmnt(f"{dev} -o TARGET -n -f")
        if not mount_points and raise_exception:
            raise MountError(f"{dev} is not mounted")

        return mount_points

    @classmethod
    def get_dev_by_mount_point(cls, mount_point: str) -> str:
        """Return the underlying mounted dev of the given mount_point."""
        return cls._findmnt(f"-no SOURCE {mount_point}")

    @classmethod
    def is_target_mounted(cls, target: Union[Path, str]) -> bool:
        return cls._findmnt(f"{target}") != ""

    @classmethod
    def get_parent_dev(cls, child_device: str) -> str:
        """
        When `/dev/nvme0n1p1` is specified as child_device, /dev/nvme0n1 is returned.

        cmd params:
            -d: print the result from specified dev only.
        """
        cmd = f"-idpn -o PKNAME {child_device}"
        if res := cls._lsblk(cmd):
            return res
        else:
            raise ValueError(f"{child_device} not found or not a partition")

    @classmethod
    def get_dev_family(cls, parent_device: str, *, include_parent=True) -> List[str]:
        """
        When `/dev/nvme0n1` is specified as parent_device,
        ["/dev/nvme0n1", "/dev/nvme0n1p1", "/dev/nvme0n1p2"...] will be return
        """
        cmd = f"-Pp -o NAME {parent_device}"
        res = list(
            map(
                lambda l: l.split("=")[-1].strip('"'),
                cls._lsblk(cmd).splitlines(),
            )
        )

        # the first line is the parent of the dev family
        if include_parent:
            return res
        else:
            return res[1:]

    @classmethod
    def get_dev_size(cls, dev: str) -> int:
        """Return the size of dev by bytes.

        NOTE:
            -d: print the result from specified dev only
            -b: print size in bytes
            -n: no headings
        """
        cmd = f"-dbn -o SIZE {dev}"
        try:
            return int(cls._lsblk(cmd))
        except ValueError:
            raise ValueError(f"failed to get size of {dev}") from None

    @classmethod
    def mount_rw(cls, target: str, mount_point: Union[Path, str]):
        """
        NOTE: pass args = ["--make-private", "--make-unbindable"] to prevent
        mount events propagation to/from this mount point.
        """
        options = ["rw"]
        args = ["--make-private", "--make-unbindable"]
        cls._mount(target, str(mount_point), options=options, args=args)

    @classmethod
    def bind_mount_ro(cls, target: str, mount_point: Union[Path, str]):
        """
        NOTE: pass args = ["--make-private", "--make-unbindable"] to prevent
        mount events propagation to/from this mount point.
        """
        options = ["bind", "ro"]
        args = ["--make-private", "--make-unbindable"]
        cls._mount(target, str(mount_point), options=options, args=args)

    @classmethod
    def umount(cls, target: Union[Path, str], *, ignore_error=False):
        # first try to check whether the target(either a mount point or a dev)
        # is mounted
        if not cls.is_target_mounted(target):
            return

        # if the target is mounted, try to unmount it.
        try:
            _cmd = f"umount -f {target}"
            subprocess_call(_cmd, raise_exception=True)
        except CalledProcessError as e:
            _failure_msg = (
                f"failed to umount {target}: {e.returncode=}, {e.stderr=}, {e.stdout=}"
            )
            logger.warning(_failure_msg)

            if not ignore_error:
                raise BootControlError(_failure_msg) from None

    @classmethod
    def mkfs_ext4(cls, dev: str):
        _specify_uuid = ""
        try:
            # inherit previous uuid
            _specify_uuid = f"-U {cls.get_uuid_by_dev(dev)}"
        except ValueError:
            pass

        try:
            logger.warning(f"format {dev} to ext4...")
            _cmd = f"mkfs.ext4 {_specify_uuid} {dev}"
            subprocess_call(_cmd, raise_exception=True)
        except CalledProcessError as e:
            _failure_msg = f"failed to apply mkfs.ext4 on {dev}: {e!r}"
            logger.error(_failure_msg)
            raise MkfsError(_failure_msg)

    @classmethod
    def mount_refroot(
        cls,
        standby_slot_dev: str,
        active_slot_dev: str,
        refroot_mount_point: str,
        *,
        standby_as_ref: bool,
    ):
        """Mount reference rootfs that we copy files from to <refroot_mount_point>.

        This method bind mount refroot as ro with make-private flag and make-unbindable flag,
        to prevent ANY accidental writes/changes to the refroot.
        """
        _refroot_dev = standby_slot_dev if standby_as_ref else active_slot_dev

        # NOTE: set raise_exception to false to allow not mounted
        # not mounted dev will have empty return str
        if _refroot_active_mount_point := CMDHelperFuncs.get_mount_point_by_dev(
            _refroot_dev, raise_exception=False
        ):
            CMDHelperFuncs.bind_mount_ro(
                _refroot_active_mount_point,
                refroot_mount_point,
            )
        else:
            # NOTE: refroot is expected to be mounted,
            # if refroot is active rootfs, it is mounted as /;
            # if refroot is standby rootfs(in-place update mode),
            # it will be mounted to /mnt/standby(rw), so we still mount it as bind,ro
            raise MountError("refroot is expected to be mounted")

    @classmethod
    def mount_ro(cls, *, target: str, mount_point: Union[str, Path]):
        """Mount target on mount_point ro.

        If the target is already mounted(i.e., when the target is active_slot),
        we try to bind mount the dev. Otherwise, directly mount it.

        This method bind mount the target as ro with make-private flag and make-unbindable flag,
        to prevent ANY accidental writes/changes to the target.
        """
        # NOTE: set raise_exception to false to allow not mounted
        #       not mounted dev will have empty return str
        if _active_mount_point := CMDHelperFuncs.get_mount_point_by_dev(
            target, raise_exception=False
        ):
            CMDHelperFuncs.bind_mount_ro(
                _active_mount_point,
                mount_point,
            )
        else:
            # target is not mounted, we mount it by ourself
            options = ["ro"]
            args = ["--make-private", "--make-unbindable"]
            cls._mount(target, str(mount_point), options=options, args=args)

    @classmethod
    def reboot(cls):
        try:
            subprocess_call("reboot", raise_exception=True)
        except CalledProcessError:
            logger.exception("failed to reboot")
            raise


###### helper mixins ######


class OTAStatusFilesControl:
    """Logics for controlling ota_status files, including status, slot_in_use, version.

    OTAStatus files:
        status: current slot's OTA status
        slot_in_use: expected active slot
        version: image version in current slot

    If status or slot_in_use file is missing, initialization is required.
    status will be set to INITIALIZED, and slot_in_use will be set to current slot.
    """

    def __init__(
        self,
        *,
        active_slot: str,
        standby_slot: str,
        current_ota_status_dir: Union[str, Path],
        standby_ota_status_dir: Union[str, Path],
        finalize_switching_boot: Callable[[], None],
    ) -> None:
        self.active_slot = active_slot
        self.standby_slot = standby_slot
        self.current_ota_status_dir = Path(current_ota_status_dir)
        self.standby_ota_status_dir = Path(standby_ota_status_dir)
        self.finalize_switching_boot = finalize_switching_boot
        self._ota_status = self._init_ota_status_files()

    def _init_ota_status_files(self) -> wrapper.StatusOta:
        """Check and/or init ota_status files for current slot."""
        self.current_ota_status_dir.mkdir(exist_ok=True, parents=True)

        # parse ota_status and slot_in_use
        _ota_status = self._load_current_status()
        _slot_in_use = self._load_current_slot_in_use()
        if not (_ota_status and _slot_in_use):
            logger.info(
                "ota_status files incompleted/not presented, "
                "initializing boot control files..."
            )
            _ota_status = wrapper.StatusOta.INITIALIZED
            self._store_current_slot_in_use(self.active_slot)
            self._store_current_status(wrapper.StatusOta.INITIALIZED)
        elif _ota_status == wrapper.StatusOta.UPDATING:
            if self._is_switching_boot(self.active_slot):
                self.finalize_switching_boot()
                _ota_status = wrapper.StatusOta.SUCCESS
            else:
                _ota_status = wrapper.StatusOta.FAILURE
        elif _ota_status == wrapper.StatusOta.ROLLBACKING:
            if self._is_switching_boot(self.active_slot):
                self.finalize_switching_boot()
                _ota_status = wrapper.StatusOta.SUCCESS
            else:
                _ota_status = wrapper.StatusOta.ROLLBACK_FAILURE
        # status except UPDATING/ROLLBACKING remained as it

        # detect failed reboot, but only print error logging currently
        if (
            _ota_status != wrapper.StatusOta.INITIALIZED
            and _slot_in_use != self.active_slot
        ):
            logger.error(
                f"boot into old slot {self.active_slot}, "
                f"but slot_in_use indicates it should boot into {_slot_in_use}, "
                "this might indicate a failed finalization at first reboot after update/rollback"
            )

        logger.info(f"boot control init finished, ota_status is {_ota_status}")
        # store the ota_status to current disk
        self._store_current_status(_ota_status)
        # return ota_status for this inst
        return _ota_status

    # slot_in_use control

    def _store_current_slot_in_use(self, _slot: str):
        write_str_to_file_sync(
            self.current_ota_status_dir / cfg.SLOT_IN_USE_FNAME, _slot
        )

    def _store_standby_slot_in_use(self, _slot: str):
        write_str_to_file_sync(
            self.standby_ota_status_dir / cfg.SLOT_IN_USE_FNAME, _slot
        )

    def _load_current_slot_in_use(self) -> Optional[str]:
        if res := read_str_from_file(
            self.current_ota_status_dir / cfg.SLOT_IN_USE_FNAME, default=""
        ):
            return res

    # status control

    def _store_current_status(self, _status: wrapper.StatusOta):
        write_str_to_file_sync(
            self.current_ota_status_dir / cfg.OTA_STATUS_FNAME, _status.name
        )

    def _store_standby_status(self, _status: wrapper.StatusOta):
        write_str_to_file_sync(
            self.standby_ota_status_dir / cfg.OTA_STATUS_FNAME, _status.name
        )

    def _load_current_status(self) -> Optional[wrapper.StatusOta]:
        if _status_str := read_str_from_file(
            self.current_ota_status_dir / cfg.OTA_STATUS_FNAME
        ).upper():
            try:
                return wrapper.StatusOta[_status_str]
            except KeyError:
                pass  # invalid status string

    # version control

    def _store_standby_version(self, _version: str):
        write_str_to_file_sync(
            self.standby_ota_status_dir / cfg.OTA_VERSION_FNAME,
            _version,
        )

    # helper methods

    def _is_switching_boot(self, active_slot: str) -> bool:
        """Detect whether we should switch boot or not with ota_status files."""
        # evidence: ota_status
        _is_updating_or_rollbacking = self._load_current_status() in [
            wrapper.StatusOta.UPDATING,
            wrapper.StatusOta.ROLLBACKING,
        ]

        # evidence: slot_in_use
        _is_switching_slot = self._load_current_slot_in_use() == active_slot

        logger.info(
            f"switch_boot detecting result:"
            f"{_is_updating_or_rollbacking=}, "
            f"{_is_switching_slot=}"
        )
        return _is_updating_or_rollbacking and _is_switching_slot

    # public methods

    # boot control used methods

    def pre_update_current(self):
        """On pre_update stage, set current slot's status to FAILURE
        and set slot_in_use to standby slot."""
        self._store_current_status(wrapper.StatusOta.FAILURE)
        self._store_current_slot_in_use(self.standby_slot)

    def pre_update_standby(self, *, version: str):
        """On pre_update stage, set standby slot's status to UPDATING,
        set slot_in_use to standby slot, and set version.

        NOTE: expecting standby slot to be mounted and ready for use!
        """
        # create the ota-status folder unconditionally
        self.standby_ota_status_dir.mkdir(exist_ok=True, parents=True)
        # store status to standby slot
        self._store_standby_status(wrapper.StatusOta.UPDATING)
        self._store_standby_version(version)
        self._store_standby_slot_in_use(self.standby_slot)

    def pre_rollback_current(self):
        self._store_current_status(wrapper.StatusOta.FAILURE)

    def pre_rollback_standby(self):
        # store ROLLBACKING status to standby
        self.standby_ota_status_dir.mkdir(exist_ok=True, parents=True)
        self._store_standby_status(wrapper.StatusOta.ROLLBACKING)

    def load_version(self) -> str:
        _version = read_str_from_file(
            self.current_ota_status_dir / cfg.OTA_VERSION_FNAME,
            missing_ok=True,
            default="",
        )
        if not _version:
            logger.warning("version file not found, return empty version string")

        return _version

    def on_failure(self):
        """Store FAILURE to status file on failure."""
        self._store_current_status(wrapper.StatusOta.FAILURE)
        # when standby slot is not created, otastatus is not needed to be set
        if self.standby_ota_status_dir.is_dir():
            self._store_standby_status(wrapper.StatusOta.FAILURE)

    @property
    def ota_status(self) -> wrapper.StatusOta:
        """Read only ota_status property."""
        return self._ota_status


class SlotInUseMixin:
    current_ota_status_dir: Path
    standby_ota_status_dir: Path

    def _store_current_slot_in_use(self, _slot: str):
        write_str_to_file_sync(
            self.current_ota_status_dir / cfg.SLOT_IN_USE_FNAME, _slot
        )

    def _store_standby_slot_in_use(self, _slot: str):
        write_str_to_file_sync(
            self.standby_ota_status_dir / cfg.SLOT_IN_USE_FNAME, _slot
        )

    def _load_current_slot_in_use(self) -> Optional[str]:
        if res := read_str_from_file(
            self.current_ota_status_dir / cfg.SLOT_IN_USE_FNAME, default=""
        ):
            return res


class OTAStatusMixin:
    current_ota_status_dir: Path
    standby_ota_status_dir: Path
    ota_status: wrapper.StatusOta

    def _store_current_ota_status(self, _status: wrapper.StatusOta):
        write_str_to_file_sync(
            self.current_ota_status_dir / cfg.OTA_STATUS_FNAME, _status.name
        )

    def _store_standby_ota_status(self, _status: wrapper.StatusOta):
        write_str_to_file_sync(
            self.standby_ota_status_dir / cfg.OTA_STATUS_FNAME, _status.name
        )

    def _load_current_ota_status(self) -> Optional[wrapper.StatusOta]:
        if _status_str := read_str_from_file(
            self.current_ota_status_dir / cfg.OTA_STATUS_FNAME
        ).upper():
            try:
                return wrapper.StatusOta[_status_str]
            except KeyError:
                pass  # invalid status string

    def get_ota_status(self) -> wrapper.StatusOta:
        return self.ota_status


class VersionControlMixin:
    current_ota_status_dir: Path
    standby_ota_status_dir: Path

    def _store_standby_version(self, _version: str):
        write_str_to_file_sync(
            self.standby_ota_status_dir / cfg.OTA_VERSION_FNAME,
            _version,
        )

    def load_version(self) -> str:
        _version = read_str_from_file(
            self.current_ota_status_dir / cfg.OTA_VERSION_FNAME,
            missing_ok=True,
            default="",
        )
        if not _version:
            logger.warning("version file not found, return empty version string")

        return _version


class PrepareMountMixin:
    standby_slot_mount_point: Path
    ref_slot_mount_point: Path

    def _prepare_mount_points(self):
        self.standby_slot_mount_point.mkdir(exist_ok=True, parents=True)
        self.ref_slot_mount_point.mkdir(exist_ok=True, parents=True)

    def _prepare_and_mount_standby(self, standby_slot_dev: str, *, erase=False):
        self.standby_slot_mount_point.mkdir(parents=True, exist_ok=True)

        # first try umount the dev
        CMDHelperFuncs.umount(standby_slot_dev)

        # format the whole standby slot if needed
        if erase:
            logger.warning(f"perform mkfs.ext4 on standby slot({standby_slot_dev})")
            CMDHelperFuncs.mkfs_ext4(standby_slot_dev)

        # try to mount the standby dev
        CMDHelperFuncs.mount_rw(standby_slot_dev, self.standby_slot_mount_point)

    def _mount_refroot(
        self,
        *,
        standby_dev: str,
        active_dev: str,
        standby_as_ref: bool,
    ):
        CMDHelperFuncs.umount(self.ref_slot_mount_point)
        CMDHelperFuncs.mount_refroot(
            standby_slot_dev=standby_dev,
            active_slot_dev=active_dev,
            refroot_mount_point=str(self.ref_slot_mount_point),
            standby_as_ref=standby_as_ref,
        )

    def _umount_all(self, *, ignore_error: bool = False):
        CMDHelperFuncs.umount(self.standby_slot_mount_point, ignore_error=ignore_error)
        CMDHelperFuncs.umount(self.ref_slot_mount_point, ignore_error=ignore_error)


class PrepareMountHelper:
    """Helper class that provides methods for mounting."""

    def __init__(
        self,
        *,
        standby_slot_mount_point: Union[str, Path],
        active_slot_mount_point: Union[str, Path],
    ) -> None:
        self.standby_slot_mount_point = Path(standby_slot_mount_point)
        self.active_slot_mount_point = Path(active_slot_mount_point)
        self.standby_slot_mount_point.mkdir(exist_ok=True, parents=True)
        self.active_slot_mount_point.mkdir(exist_ok=True, parents=True)
        self.standby_boot_dir = self.standby_slot_mount_point / "boot"

    def mount_standby(self, standby_slot_dev: str, *, erase=False):
        # first try umount the dev
        CMDHelperFuncs.umount(standby_slot_dev)
        # format the whole standby slot if needed
        if erase:
            logger.warning(f"perform mkfs.ext4 on standby slot({standby_slot_dev})")
            CMDHelperFuncs.mkfs_ext4(standby_slot_dev)
        # try to mount the standby dev
        CMDHelperFuncs.mount_rw(standby_slot_dev, self.standby_slot_mount_point)

    def mount_active(self, active_slot_dev: str):
        # first try umount the dev
        CMDHelperFuncs.umount(self.active_slot_mount_point)
        # mount active slot ro, unpropagated
        CMDHelperFuncs.mount_ro(
            target=active_slot_dev,
            mount_point=self.active_slot_mount_point,
        )

    def umount_all(self, *, ignore_error: bool = False):
        CMDHelperFuncs.umount(self.standby_slot_mount_point, ignore_error=ignore_error)
        CMDHelperFuncs.umount(self.active_slot_mount_point, ignore_error=ignore_error)


def cat_proc_cmdline(target: str = "/proc/cmdline") -> str:
    return read_str_from_file(target, missing_ok=False)
