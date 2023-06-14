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


import shutil
from pathlib import Path
from subprocess import CalledProcessError
from typing import List, Optional, Union, Callable

from ._errors import (
    BootControlError,
    MountError,
    MkfsError,
    MountFailedReason,
)

from .. import log_setting
from ..configs import config as cfg
from ..common import (
    read_str_from_file,
    subprocess_call,
    subprocess_check_output,
    write_str_to_file_sync,
)
from ..errors import BootControlInitError
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
        """lsblk command wrapper.

        Default return empty str if raise_exception==False.
        """
        _cmd = f"lsblk {args}"
        return subprocess_check_output(
            _cmd,
            raise_exception=raise_exception,
            default="",
        )

    ###### derived helper methods ######

    @classmethod
    def get_fslabel_by_dev(cls, dev: str) -> str:
        """Return the fslabel of the dev if any or empty str."""
        args = f"-in -o LABEL {dev}"
        return cls._lsblk(args)

    @classmethod
    def get_partuuid_by_dev(cls, dev: str) -> str:
        """Return partuuid of input device."""
        args = f"-in -o PARTUUID {dev}"
        try:
            return cls._lsblk(args, raise_exception=True)
        except Exception as e:
            msg = f"failed to get partuuid for {dev}: {e!r}"
            raise ValueError(msg) from None

    @classmethod
    def get_uuid_by_dev(cls, dev: str) -> str:
        """Return uuid of input device."""
        args = f"-in -o UUID {dev}"
        try:
            return cls._lsblk(args, raise_exception=True)
        except Exception as e:
            msg = f"failed to get uuid for {dev}: {e!r}"
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
                lambda line: line.split("=")[-1].strip('"'),
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
    def set_dev_fslabel(cls, dev: str, fslabel: str):
        cmd = f"e2label {dev} {fslabel}"
        try:
            subprocess_call(cmd, raise_exception=True)
        except Exception as e:
            raise ValueError(f"failed to set {fslabel=} to {dev=}: {e!r}") from None

    @classmethod
    def mount_rw(cls, target: str, mount_point: Union[Path, str]):
        """Mount the target to the mount_point read-write.

        NOTE: pass args = ["--make-private", "--make-unbindable"] to prevent
              mount events propagation to/from this mount point.
        """
        options = ["rw"]
        args = ["--make-private", "--make-unbindable"]
        cls._mount(target, str(mount_point), options=options, args=args)

    @classmethod
    def bind_mount_ro(cls, target: str, mount_point: Union[Path, str]):
        """Bind mount the target to the mount_point read-only.

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
            _cmd = f"umount -l {target}"
            subprocess_call(_cmd, raise_exception=True)
        except CalledProcessError as e:
            _failure_msg = (
                f"failed to umount {target}: {e.returncode=}, {e.stderr=}, {e.stdout=}"
            )
            logger.warning(_failure_msg)

            if not ignore_error:
                raise BootControlError(_failure_msg) from None

    @classmethod
    def mkfs_ext4(cls, dev: str, *, fslabel: Optional[str] = None):
        # NOTE: preserve the UUID and FSLABEL(if set)
        _specify_uuid = ""
        try:
            _specify_uuid = f"-U {cls.get_uuid_by_dev(dev)}"
        except Exception:
            pass

        # if fslabel is specified, then use it,
        # otherwise try to detect the previously set one
        _specify_fslabel = ""
        if fslabel:
            _specify_fslabel = f"-L {fslabel}"
        elif _fslabel := cls.get_fslabel_by_dev(dev):
            _specify_fslabel = f"-L {_fslabel}"

        try:
            logger.warning(f"format {dev} to ext4...")
            _cmd = f"mkfs.ext4 {_specify_uuid} {_specify_fslabel} {dev}"
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
        """Mount target on mount_point read-only.

        If the target device is mounted, we bind mount the target device to mount_point,
        if the target device is not mounted, we directly mount it to the mount_point.

        This method mount the target as ro with make-private flag and make-unbindable flag,
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
        """Reboot the whole system otaclient running at and terminate otaclient.

        NOTE(20230614): this command MUST also make otaclient exit immediately.
        """
        os.sync()
        try:
            subprocess_call("reboot", raise_exception=True)
            sys.exit(0)
        except CalledProcessError:
            logger.exception("failed to reboot")
            raise


###### helper mixins ######

FinalizeSwitchBootFunc = Callable[[], bool]


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
        finalize_switching_boot: FinalizeSwitchBootFunc,
    ) -> None:
        self.active_slot = active_slot
        self.standby_slot = standby_slot
        self.current_ota_status_dir = Path(current_ota_status_dir)
        self.standby_ota_status_dir = Path(standby_ota_status_dir)
        self.finalize_switching_boot = finalize_switching_boot

        # NOTE: pre-assign live ota_status with the loaded ota_status,
        #       and then update live ota_status in below.
        #       The reason is for some platform, like raspberry pi 4B,
        #       the finalize_switching_boot might be slow, so we first provide
        #       live ota_status the same as loaded ota_status(or INITIALIZED),
        #       then update it after the init_ota_status_files finished.
        _loaded_ota_status = self._load_current_status()
        self._ota_status = (
            _loaded_ota_status
            if _loaded_ota_status is not None
            else wrapper.StatusOta.INITIALIZED
        )
        self._init_ota_status_files()
        logger.info(
            f"ota_status files parsing completed, ota_status is {self._ota_status}"
        )

    def _init_ota_status_files(self):
        """Check and/or init ota_status files for current slot."""
        self.current_ota_status_dir.mkdir(exist_ok=True, parents=True)

        # load ota_status and slot_in_use file
        _loaded_ota_status = self._load_current_status()
        _loaded_slot_in_use = self._load_current_slot_in_use()

        # initialize ota_status files if not presented/incompleted/invalid
        if not (_loaded_ota_status and _loaded_slot_in_use):
            logger.info(
                "ota_status files incompleted/not presented, "
                "initializing and set/store status to INITIALIZED..."
            )
            self._store_current_slot_in_use(self.active_slot)
            self._store_current_status(wrapper.StatusOta.INITIALIZED)
            self._ota_status = wrapper.StatusOta.INITIALIZED
            return

        # updating or rollbacking,
        # with status==UPDATING or ROLLBACKING, we are expecting this is the first reboot
        # during switching boot, and we should try finalizing the switch boot.
        if _loaded_ota_status in [
            wrapper.StatusOta.UPDATING,
            wrapper.StatusOta.ROLLBACKING,
        ]:
            # finalization failed or not in switching boot
            if not (
                self._is_switching_boot(self.active_slot)
                and self.finalize_switching_boot()
            ):
                self._ota_status = (
                    wrapper.StatusOta.ROLLBACK_FAILURE
                    if _loaded_ota_status == wrapper.StatusOta.ROLLBACKING
                    else wrapper.StatusOta.FAILURE
                )
                self._store_current_status(self._ota_status)
                _err_msg = "finalization failed on first reboot during switching boot"
                logger.error(_err_msg)
                raise BootControlInitError(_err_msg)

            # finalizing switch boot successfully
            self._ota_status = wrapper.StatusOta.SUCCESS
            self._store_current_status(wrapper.StatusOta.SUCCESS)
            return

        # status except UPDATING and ROLLBACKING(like SUCCESS/FAILURE/ROLLBACK_FAILURE)
        # are remained as it
        else:
            self._ota_status = _loaded_ota_status
            # detect failed reboot, but only print error logging currently
            if _loaded_slot_in_use != self.active_slot:
                logger.error(
                    f"boot into old slot {self.active_slot}, "
                    f"but slot_in_use indicates it should boot into {_loaded_slot_in_use}, "
                    "this might indicate a failed finalization at first reboot after update/rollback"
                )

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

    def load_active_slot_version(self) -> str:
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
        CMDHelperFuncs.mount_refroot(
            standby_slot_dev=standby_dev,
            active_slot_dev=active_dev,
            refroot_mount_point=str(self.ref_slot_mount_point),
            standby_as_ref=standby_as_ref,
        )

    def _umount_all(self, *, ignore_error: bool = False):
        CMDHelperFuncs.umount(self.standby_slot_mount_point, ignore_error=ignore_error)
        CMDHelperFuncs.umount(self.ref_slot_mount_point, ignore_error=ignore_error)


class SlotMountHelper:
    """Helper class that provides methods for mounting slots."""

    def __init__(
        self,
        *,
        standby_slot_dev: Union[str, Path],
        standby_slot_mount_point: Union[str, Path],
        active_slot_dev: Union[str, Path],
        active_slot_mount_point: Union[str, Path],
    ) -> None:
        # dev
        self.standby_slot_dev = str(standby_slot_dev)
        self.active_slot_dev = str(active_slot_dev)
        # mount points
        self.standby_slot_mount_point = Path(standby_slot_mount_point)
        self.active_slot_mount_point = Path(active_slot_mount_point)
        self.standby_slot_mount_point.mkdir(exist_ok=True, parents=True)
        self.active_slot_mount_point.mkdir(exist_ok=True, parents=True)
        # standby slot /boot dir
        self.standby_boot_dir = self.standby_slot_mount_point / "boot"

    def mount_standby(self, *, raise_exc: bool = True) -> bool:
        """Mount standby slot dev to <standby_slot_mount_point>.

        Args:
            erase_standby: whether to format standby slot dev to ext4 before mounting.
            raise_exc: if exception occurs, raise it or not.

        Return:
            A bool indicates whether the mount succeeded or not.
        """
        logger.debug("mount standby slot rootfs dev...")
        try:
            # first try umount mount point and dev
            CMDHelperFuncs.umount(self.standby_slot_mount_point, ignore_error=True)
            if CMDHelperFuncs.is_target_mounted(self.standby_slot_dev):
                CMDHelperFuncs.umount(self.standby_slot_dev)

            # try to mount the standby dev
            CMDHelperFuncs.mount_rw(
                target=self.standby_slot_dev,
                mount_point=self.standby_slot_mount_point,
            )
            return True
        except Exception:
            if raise_exc:
                raise
            return False

    def mount_active(self, *, raise_exc: bool = True) -> bool:
        """Mount active rootfs ready-only.

        Args:
            raise_exc: if exception occurs, raise it or not.

        Return:
            A bool indicates whether the mount succeeded or not.
        """
        # first try umount the mount_point
        logger.debug("mount active slot rootfs dev...")
        try:
            CMDHelperFuncs.umount(self.active_slot_mount_point, ignore_error=True)
            # mount active slot ro, unpropagated
            CMDHelperFuncs.mount_ro(
                target=self.active_slot_dev,
                mount_point=self.active_slot_mount_point,
            )
            return True
        except Exception:
            if raise_exc:
                raise
            return False

    def preserve_ota_folder_to_standby(self):
        """Copy the /boot/ota folder to standby slot to preserve it.

        /boot/ota folder contains the ota setting for this device,
        so we should preserve it for each slot, accross each update.
        """
        logger.debug("copy /boot/ota from active to standby.")
        try:
            _src = self.active_slot_mount_point / Path(cfg.OTA_DIR).relative_to("/")
            _dst = self.standby_slot_mount_point / Path(cfg.OTA_DIR).relative_to("/")
            shutil.copytree(_src, _dst, dirs_exist_ok=True)
        except Exception as e:
            raise ValueError(f"failed to copy /boot/ota from active to standby: {e!r}")

    def umount_all(self, *, ignore_error: bool = False):
        logger.debug("unmount standby slot and active slot mount point...")
        CMDHelperFuncs.umount(self.standby_slot_mount_point, ignore_error=ignore_error)
        CMDHelperFuncs.umount(self.active_slot_mount_point, ignore_error=ignore_error)


def cat_proc_cmdline(target: str = "/proc/cmdline") -> str:
    return read_str_from_file(target, missing_ok=False)
