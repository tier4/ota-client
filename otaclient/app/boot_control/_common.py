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


from __future__ import annotations
import logging
import shutil
import sys
from pathlib import Path
from subprocess import CalledProcessError
from typing import Literal, Optional, Union, Callable, NoReturn

from ..configs import config as cfg
from ..common import (
    read_str_from_file,
    subprocess_call,
    subprocess_check_output,
    write_str_to_file_sync,
)
from ..proto import wrapper


logger = logging.getLogger(__name__)

# fmt: off
PartitionToken = Literal[
    "UUID", "PARTUUID",
    "LABEL", "PARTLABEL",
    "TYPE",
]
# fmt: on


class CMDHelperFuncs:
    """HelperFuncs bundle for wrapped linux cmd."""

    @classmethod
    def get_attrs_by_dev(
        cls, attr: PartitionToken, dev: Path | str, *, raise_exception: bool = True
    ) -> Optional[str]:
        """Get <attr> from <dev>.

        This is implemented by calling:
            lsblk -in -o <attr> <dev>
        """
        cmd = ["lsblk", "-ino", attr, str(dev)]
        return subprocess_check_output(cmd, raise_exception=raise_exception)

    @classmethod
    def get_dev_by_token(
        cls, token: PartitionToken, value: str, *, raise_exception: bool = True
    ) -> Optional[list[str]]:
        """Get a list of device(s) that matches the token=value pair.

        This is implemented by calling:
            blkid -o device -t <TOKEN>=<VALUE>
        """
        cmd = ["blkid", "-o", "device", "-t", f"{token}={value}"]
        if res := subprocess_check_output(cmd, raise_exception=raise_exception):
            return res.splitlines()

    @classmethod
    def get_current_rootfs_dev(cls, *, raise_exception: bool = True) -> str:
        """Get the devpath of current rootfs dev.

        This is implemented by calling
            findmnt -nfc -o SOURCE /

        Returns:
            full path to dev of the current rootfs.

        Raises:
            subprocess.CalledProcessError on failed command call.
        """
        cmd = ["findmnt", "-nfco", "SOURCE", cfg.ACTIVE_ROOTFS_PATH]
        return subprocess_check_output(cmd, raise_exception=raise_exception)

    @classmethod
    def get_mount_point_by_dev(
        cls, dev: str, *, raise_exception: bool = True
    ) -> Optional[str]:
        """Get the FIRST mountpoint of the <dev>.

        This is implemented by calling:
            findmnt <dev> -nfo TARGET <dev>

        NOTE: findmnt raw result might have multiple lines if target dev is bind mounted.
            use option -f to only show the first file system.
        """
        cmd = ["findmnt", "-nfo", "TARGET", dev]
        return subprocess_check_output(cmd, raise_exception=raise_exception)

    @classmethod
    def get_dev_by_mount_point(
        cls, mount_point: str, *, raise_exception: bool = True
    ) -> Optional[str]:
        """Return the underlying mounted dev of the given mount_point.

        This is implemented by calling:
            findmnt -no SOURCE <mount_point>
        """
        cmd = ["findmnt", "-no", "SOURCE", mount_point]
        return subprocess_check_output(cmd, raise_exception=raise_exception)

    @classmethod
    def is_target_mounted(
        cls, target: Path | str, *, raise_exception: bool = True
    ) -> bool:
        """Check if <target> is mounted or not. <target> can be a dev or a mount point.

        This is implemented by calling:
            findmnt <target>

            and see if there is any matching device.
        """
        cmd = ["findmnt", target]
        return bool(subprocess_check_output(cmd, raise_exception=raise_exception))

    @classmethod
    def get_parent_dev(
        cls, child_device: str, *, raise_exception: bool = True
    ) -> Optional[str]:
        """Get the parent devpath from <child_device>.
        When `/dev/nvme0n1p1` is specified as child_device, /dev/nvme0n1 is returned.

        This function is implemented by calling:
            lsblk -idpno PKNAME <child_device>
        """
        cmd = ["lsblk", "-idpno", "PKNAME", child_device]
        return subprocess_check_output(cmd, raise_exception=raise_exception)

    @classmethod
    def set_ext4_fslabel(cls, dev: str, fslabel: str, *, raise_exception: bool = True):
        cmd = ["e2label", dev, fslabel]
        subprocess_call(cmd, raise_exception=raise_exception)

    @classmethod
    def mount_rw(cls, target: str, mount_point: Path | str):
        """Mount the target to the mount_point read-write.

        This is implemented by calling:
            mount -o rw --make-private --make-unbindable <target> <mount_point>

        NOTE: pass args = ["--make-private", "--make-unbindable"] to prevent
              mount events propagation to/from this mount point.

        Raises:
            CalledProcessError on failed mount.
        """
        # fmt: off
        cmd = [
            "mount",
            "-o", "rw",
            "--make-private", "--make-unbindable",
            target,
            str(mount_point),
        ]
        # fmt: on
        subprocess_call(cmd, raise_exception=True)

    @classmethod
    def bind_mount_ro(cls, target: str, mount_point: Path | str):
        """Bind mount the target to the mount_point read-only.

        This is implemented by calling:
            mount -o bind,ro --make-private --make-unbindable <target> <mount_point>

        Raises:
            CalledProcessError on failed mount.
        """
        # fmt: off
        cmd = [
            "mount",
            "-o", "bind,ro",
            "--make-private", "--make-unbindable",
            target,
            str(mount_point)
        ]
        # fmt: on
        subprocess_call(cmd, raise_exception=True)

    @classmethod
    def umount(cls, target: Path | str, *, ignore_error=False):
        """Try to unmount the <target>.

        This function will first check whether the <target> is mounted.
        """
        # first try to check whether the target(either a mount point or a dev)
        # is mounted
        if not cls.is_target_mounted(target, raise_exception=False):
            return

        # if the target is mounted, try to unmount it.
        try:
            _cmd = ["umount", str(target)]
            subprocess_call(_cmd, raise_exception=True)
        except CalledProcessError as e:
            if not ignore_error:
                _failure_msg = (
                    f"failed to umount {target}: {e!r}\n"
                    f"retcode={e.returncode}, \n"
                    f"stderr={e.stderr.decode()}, \n"
                    f"stdout={e.stdout.decode()}"
                )
                logger.warning(_failure_msg)
                raise

    @classmethod
    def mkfs_ext4(
        cls, dev: str, *, fslabel: Optional[str] = None, fsuuid: Optional[str] = None
    ):
        """Call mkfs.ext4 on <dev>.

        If fslabel and/or fsuuid provided, use them in prior, otherwise
            try to preserve fsuuid and fslabel if possible.

        Raises:
            Original CalledProcessError from the failed command execution.
        """
        cmd = ["mkfs.ext4"]

        if not fsuuid:
            try:
                fsuuid = cls.get_attrs_by_dev("UUID", dev, raise_exception=True)
                assert fsuuid
                logger.debug(f"reuse previous UUID: {fsuuid}")
            except Exception:
                pass
        if fsuuid:
            logger.debug(f"using UUID: {fsuuid}")
            cmd.extend(["-U", fsuuid])

        if not fslabel:
            try:
                fslabel = cls.get_attrs_by_dev("LABEL", dev, raise_exception=True)
                assert fslabel
                logger.debug(f"reuse previous fs LABEL: {fslabel}")
            except Exception:
                pass
        if fslabel:
            logger.debug(f"using fs LABEL: {fslabel}")
            cmd.extend(["-L", fslabel])

        cmd.append(dev)
        logger.warning(f"execute {cmd=}")
        subprocess_call(cmd, raise_exception=True)

    @classmethod
    def mount_ro(cls, *, target: str, mount_point: str | Path):
        """Mount target on mount_point read-only.

        If the target device is mounted, we bind mount the target device to mount_point,
        if the target device is not mounted, we directly mount it to the mount_point.

        This method mount the target as ro with make-private flag and make-unbindable flag,
        to prevent ANY accidental writes/changes to the target.

        Raises:
            CalledProcessError on failed mounting.
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
            # fmt: off
            cmd = [
                "mount",
                "-o", "ro",
                "--make-private", "--make-unbindable",
                target,
                str(mount_point),
            ]
            # fmt: on
            subprocess_call(cmd, raise_exception=True)

    @classmethod
    def reboot(cls, args: Optional[list[str]] = None) -> NoReturn:
        """Reboot the whole system otaclient running at and terminate otaclient.

        NOTE(20230614): this command MUST also make otaclient exit immediately.
        NOTE(20240421): rpi_boot's reboot takes args.
        """
        cmd = ["reboot"]
        if args:
            cmd.extend(args)

        try:
            subprocess_call(cmd, raise_exception=True)
            sys.exit(0)
        except CalledProcessError:
            logger.exception("failed to reboot")
            raise


FinalizeSwitchBootFunc = Callable[[], bool]


class OTAStatusFilesControl:
    """Logics for controlling otaclient's OTA status with corresponding files.

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
        force_initialize: bool = False,
    ) -> None:
        self.active_slot = active_slot
        self.standby_slot = standby_slot
        self.current_ota_status_dir = Path(current_ota_status_dir)
        self.standby_ota_status_dir = Path(standby_ota_status_dir)
        self.finalize_switching_boot = finalize_switching_boot

        self._force_initialize = force_initialize
        self.current_ota_status_dir.mkdir(exist_ok=True, parents=True)
        self._load_slot_in_use_file()
        self._load_status_file()
        logger.info(
            f"ota_status files parsing completed, ota_status is {self._ota_status.name}"
        )

    def _load_status_file(self):
        """Check and/or init ota_status files for current slot."""
        _loaded_ota_status = self._load_current_status()
        if self._force_initialize:
            _loaded_ota_status = None

        # initialize ota_status files if not presented/incompleted/invalid
        if not _loaded_ota_status:
            logger.info(
                "ota_status files incompleted/not presented, "
                f"initializing and set/store status to {wrapper.StatusOta.INITIALIZED.name}..."
            )
            self._store_current_status(wrapper.StatusOta.INITIALIZED)
            self._ota_status = wrapper.StatusOta.INITIALIZED
            return
        logger.info(f"status loaded from file: {_loaded_ota_status.name}")

        # status except UPDATING and ROLLBACKING(like SUCCESS/FAILURE/ROLLBACK_FAILURE)
        # are remained as it
        if _loaded_ota_status not in [
            wrapper.StatusOta.UPDATING,
            wrapper.StatusOta.ROLLBACKING,
        ]:
            self._ota_status = _loaded_ota_status
            return

        # updating or rollbacking,
        # NOTE: pre-assign live ota_status with the loaded ota_status before entering finalizing_switch_boot,
        #       as some of the platform might have slow finalizing process(like raspberry).
        self._ota_status = _loaded_ota_status
        # if is_switching_boot, execute the injected finalize_switching_boot function from
        # boot controller, transit the ota_status according to the execution result.
        # NOTE(20230614): for boot controller during multi-stage reboot(like rpi_boot),
        #                 calling finalize_switching_boot might result in direct reboot,
        #                 in such case, otaclient will terminate and ota_status will not be updated.
        if self._is_switching_boot(self.active_slot):
            if self.finalize_switching_boot():
                self._ota_status = wrapper.StatusOta.SUCCESS
                self._store_current_status(wrapper.StatusOta.SUCCESS)
            else:
                self._ota_status = (
                    wrapper.StatusOta.ROLLBACK_FAILURE
                    if _loaded_ota_status == wrapper.StatusOta.ROLLBACKING
                    else wrapper.StatusOta.FAILURE
                )
                self._store_current_status(self._ota_status)
                logger.error(
                    "finalization failed on first reboot during switching boot"
                )

        else:
            logger.error(
                f"we are in {_loaded_ota_status.name} ota_status, "
                "but ota_status files indicate that we are not in switching boot mode, "
                "this indicates a failed first reboot"
            )
            self._ota_status = (
                wrapper.StatusOta.ROLLBACK_FAILURE
                if _loaded_ota_status == wrapper.StatusOta.ROLLBACKING
                else wrapper.StatusOta.FAILURE
            )
            self._store_current_status(self._ota_status)

    def _load_slot_in_use_file(self):
        _loaded_slot_in_use = self._load_current_slot_in_use()
        if self._force_initialize:
            _loaded_slot_in_use = None

        if not _loaded_slot_in_use:
            # NOTE(20230831): this can also resolve the backward compatibility issue
            #                 in is_switching_boot method when old otaclient doesn't create
            #                 slot_in_use file.
            self._store_current_slot_in_use(self.active_slot)
            return
        logger.info(f"slot_in_use loaded from file: {_loaded_slot_in_use}")

        # check potential failed switching boot
        if _loaded_slot_in_use and _loaded_slot_in_use != self.active_slot:
            logger.warning(
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
        return read_str_from_file(
            self.current_ota_status_dir / cfg.OTA_VERSION_FNAME,
            missing_ok=True,
            default=cfg.DEFAULT_VERSION_STR,
        )

    def on_failure(self):
        """Store FAILURE to status file on failure."""
        self._store_current_status(wrapper.StatusOta.FAILURE)
        # when standby slot is not created, otastatus is not needed to be set
        if self.standby_ota_status_dir.is_dir():
            self._store_standby_status(wrapper.StatusOta.FAILURE)

    @property
    def booted_ota_status(self) -> wrapper.StatusOta:
        """Loaded current slot's ota_status during boot control starts.

        NOTE: distinguish between the live ota_status maintained by otaclient.

        This property is only meant to be used once when otaclient starts up,
        switch to use live_ota_status by otaclient after otaclient is running.
        """
        return self._ota_status


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
        # NOTE(20230907): this will always be <standby_slot_mp>/boot,
        #                 in the future this attribute will not be used by
        #                 standby slot creater.
        self.standby_boot_dir = self.standby_slot_mount_point / Path(
            cfg.BOOT_DIR
        ).relative_to("/")

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

    def prepare_standby_dev(
        self,
        *,
        erase_standby: bool = False,
        fslabel: Optional[str] = None,
    ) -> None:
        CMDHelperFuncs.umount(self.standby_slot_dev, ignore_error=True)

        if erase_standby:
            return CMDHelperFuncs.mkfs_ext4(self.standby_slot_dev, fslabel=fslabel)

        # TODO: in the future if in-place update mode is implemented, do a
        #   fschck over the standby slot file system.
        if fslabel:
            CMDHelperFuncs.set_ext4_fslabel(self.active_slot_dev, fslabel=fslabel)

    def umount_all(self, *, ignore_error: bool = False):
        logger.debug("unmount standby slot and active slot mount point...")
        CMDHelperFuncs.umount(self.standby_slot_mount_point, ignore_error=ignore_error)
        CMDHelperFuncs.umount(self.active_slot_mount_point, ignore_error=ignore_error)


def cat_proc_cmdline(target: str = "/proc/cmdline") -> str:
    return read_str_from_file(target, missing_ok=False)
