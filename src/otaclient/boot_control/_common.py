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

import contextlib
import logging
import shutil
import sys
from pathlib import Path
from subprocess import CalledProcessError
from typing import Callable, Literal, NoReturn, Optional, Union

from otaclient.app.configs import config as cfg
from otaclient_api.v2 import types as api_types
from otaclient_common._io import read_str_from_file, write_str_to_file_atomic
from otaclient_common.common import subprocess_call, subprocess_check_output
from otaclient_common.typing import StrOrPath

logger = logging.getLogger(__name__)

# fmt: off
PartitionToken = Literal[
    "UUID", "PARTUUID",
    "LABEL", "PARTLABEL",
    "TYPE",
]
# fmt: on


class CMDHelperFuncs:
    """HelperFuncs bundle for wrapped linux cmd.

    When underlying subprocess call failed and <raise_exception> is True,
        functions defined in this class will raise the original exception
        to the upper caller.
    """

    @classmethod
    def get_attrs_by_dev(
        cls, attr: PartitionToken, dev: Path | str, *, raise_exception: bool = True
    ) -> str:
        """Get <attr> from <dev>.

        This is implemented by calling:
            `lsblk -in -o <attr> <dev>`

        Args:
            attr (PartitionToken): the attribute to retrieve from the <dev>.
            dev (Path | str): the target device path.
            raise_exception (bool, optional): raise exception on subprocess call failed.
                Defaults to True.

        Returns:
            str: <attr> of <dev>.
        """
        cmd = ["lsblk", "-ino", attr, str(dev)]
        return subprocess_check_output(cmd, raise_exception=raise_exception)

    @classmethod
    def get_dev_by_token(
        cls, token: PartitionToken, value: str, *, raise_exception: bool = True
    ) -> Optional[list[str]]:
        """Get a list of device(s) that matches the <token>=<value> pair.

        This is implemented by calling:
            blkid -o device -t <TOKEN>=<VALUE>

        Args:
            token (PartitionToken): which attribute of device to match.
            value (str): the value of the attribute.
            raise_exception (bool, optional): raise exception on subprocess call failed.
                Defaults to True.

        Returns:
            Optional[list[str]]: If there is at least one device found, return a list
                contains all found device(s), otherwise None.
        """
        cmd = ["blkid", "-o", "device", "-t", f"{token}={value}"]
        if res := subprocess_check_output(cmd, raise_exception=raise_exception):
            return res.splitlines()

    @classmethod
    def get_current_rootfs_dev(cls, *, raise_exception: bool = True) -> str:
        """Get the devpath of current rootfs dev.

        This is implemented by calling
            findmnt -nfc -o SOURCE <ACTIVE_ROOTFS_PATH>

        Args:
            raise_exception (bool, optional): raise exception on subprocess call failed.
                Defaults to True.

        Returns:
            str: the devpath of current rootfs device.
        """
        cmd = ["findmnt", "-nfco", "SOURCE", cfg.ACTIVE_ROOTFS_PATH]
        return subprocess_check_output(cmd, raise_exception=raise_exception)

    @classmethod
    def get_mount_point_by_dev(cls, dev: str, *, raise_exception: bool = True) -> str:
        """Get the FIRST mountpoint of the <dev>.

        This is implemented by calling:
            findmnt <dev> -nfo TARGET <dev>

        NOTE: option -f is used to only show the first file system.

        Args:
            dev (str): the device to check against.
            raise_exception (bool, optional): raise exception on subprocess call failed.
                Defaults to True.

        Returns:
            str: the FIRST mountpint of the <dev>, or empty string if <raise_exception> is False
                and the subprocess call failed(due to dev is not mounted or other reasons).
        """
        cmd = ["findmnt", "-nfo", "TARGET", dev]
        return subprocess_check_output(cmd, raise_exception=raise_exception)

    @classmethod
    def get_dev_by_mount_point(
        cls, mount_point: str, *, raise_exception: bool = True
    ) -> str:
        """Return the source dev of the given <mount_point>.

        This is implemented by calling:
            findmnt -no SOURCE <mount_point>

        Args:
            mount_point (str): mount_point to check against.
            raise_exception (bool, optional): raise exception on subprocess call failed.
                Defaults to True.

        Returns:
            str: the source device of <mount_point>.
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

        Args:
            target (Path | str): the target to check against. Could be a device or a mount point.
            raise_exception (bool, optional): raise exception on subprocess call failed.
                Defaults to True.

        Returns:
            bool: return True if the target has at least one mount_point.
        """
        cmd = ["findmnt", target]
        return bool(subprocess_check_output(cmd, raise_exception=raise_exception))

    @classmethod
    def get_parent_dev(cls, child_device: str, *, raise_exception: bool = True) -> str:
        """Get the parent devpath from <child_device>.

        When `/dev/nvme0n1p1` is specified as child_device, /dev/nvme0n1 is returned.

        This function is implemented by calling:
            lsblk -idpno PKNAME <child_device>

        Args:
            child_device (str): the device to find parent device from.
            raise_exception (bool, optional): raise exception on subprocess call failed.
                Defaults to True.

        Returns:
            str: the parent device of the specific <child_device>.
        """
        cmd = ["lsblk", "-idpno", "PKNAME", child_device]
        return subprocess_check_output(cmd, raise_exception=raise_exception)

    @classmethod
    def get_device_tree(
        cls, parent_dev: str, *, raise_exception: bool = True
    ) -> list[str]:
        """Get the device tree of a parent device.

        For example, for sda with 3 partitions, we will get:
        ["/dev/sda", "/dev/sda1", "/dev/sda2", "/dev/sda3"]

        This function is implemented by calling:
            lsblk -lnpo NAME <parent_dev>

        Args:
            parent_dev (str): The parent device to be checked.
            raise_exception (bool, optional): raise exception on subprocess call failed.
                Defaults to True.

        Returns:
            str: _description_
        """
        cmd = ["lsblk", "-lnpo", "NAME", parent_dev]
        raw_res = subprocess_check_output(cmd, raise_exception=raise_exception)
        return raw_res.splitlines()

    @classmethod
    def set_ext4_fslabel(cls, dev: str, fslabel: str, *, raise_exception: bool = True):
        """Set <fslabel> to ext4 formatted <dev>.

        This is implemented by calling:
            e2label <dev> <fslabel>

        Args:
            dev (str): the ext4 partition device.
            fslabel (str): the fslabel to be set.
            raise_exception (bool, optional): raise exception on subprocess call failed.
                Defaults to True.
        """
        cmd = ["e2label", dev, fslabel]
        subprocess_call(cmd, raise_exception=raise_exception)

    @classmethod
    def mount(
        cls,
        target: StrOrPath,
        mount_point: StrOrPath,
        *,
        options: list[str] | None = None,
        params: list[str] | None = None,
        raise_exception: bool = True,
    ) -> None:
        """Thin wrapper to call mount using subprocess.

        This will call the following:
            mount [-o <option1>,[<option2>[,...]] [<param1> [<param2>[...]]] <target> <mount_point>

        Args:
            target (StrOrPath): The target device to mount.
            mount_point (StrOrPath): The mount point to mount to.
            options (list[str] | None, optional): A list of options, append after -o. Defaults to None.
            params (list[str] | None, optional): A list of params. Defaults to None.
            raise_exception (bool, optional): Whether to raise exception on failed call. Defaults to True.
        """
        cmd = ["mount"]
        if options:
            cmd.extend(["-o", ",".join(options)])
        if params:
            cmd.extend(params)
        cmd = [*cmd, str(target), str(mount_point)]
        subprocess_call(cmd, raise_exception=raise_exception)

    @classmethod
    def mount_rw(
        cls, target: str, mount_point: Path | str, *, raise_exception: bool = True
    ):
        """Mount the <target> to <mount_point> read-write.

        This is implemented by calling:
            mount -o rw --make-private --make-unbindable <target> <mount_point>

        NOTE: pass args = ["--make-private", "--make-unbindable"] to prevent
              mount events propagation to/from this mount point.

        Args:
            target (str): target to be mounted.
            mount_point (Path | str): mount point to mount to.
            raise_exception (bool, optional): raise exception on subprocess call failed.
                Defaults to True.
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
        subprocess_call(cmd, raise_exception=raise_exception)

    @classmethod
    def bind_mount_ro(
        cls, target: str, mount_point: Path | str, *, raise_exception: bool = True
    ):
        """Bind mount the <target> to <mount_point> read-only.

        This is implemented by calling:
            mount -o bind,ro --make-private --make-unbindable <target> <mount_point>

        Args:
            target (str): target to be mounted.
            mount_point (Path | str): mount point to mount to.
            raise_exception (bool, optional): raise exception on subprocess call failed.
                Defaults to True.
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
        subprocess_call(cmd, raise_exception=raise_exception)

    @classmethod
    def umount(cls, target: Path | str, *, raise_exception: bool = True):
        """Try to umount the <target>.

        This is implemented by calling:
            umount <target>

        Before calling umount, the <target> will be check whether it is mounted,
            if it is not mounted, this function will return directly.

        Args:
            target (Path | str): target to be umounted.
            raise_exception (bool, optional): raise exception on subprocess call failed.
                Defaults to True.
        """
        # first try to check whether the target(either a mount point or a dev)
        # is mounted
        if not cls.is_target_mounted(target, raise_exception=False):
            return

        # if the target is mounted, try to unmount it.
        _cmd = ["umount", str(target)]
        subprocess_call(_cmd, raise_exception=raise_exception)

    @classmethod
    def mkfs_ext4(
        cls,
        dev: str,
        *,
        fslabel: Optional[str] = None,
        fsuuid: Optional[str] = None,
        raise_exception: bool = True,
    ):
        """Create new ext4 formatted filesystem on <dev>, optionally with <fslabel>
            and/or <fsuuid>.

        Args:
            dev (str): device to be formatted to ext4.
            fslabel (Optional[str], optional): fslabel of the new ext4 filesystem. Defaults to None.
                When it is None, this function will try to preserve the previous fslabel.
            fsuuid (Optional[str], optional): fsuuid of the new ext4 filesystem. Defaults to None.
                When it is None, this function will try to preserve the previous fsuuid.
            raise_exception (bool, optional): raise exception on subprocess call failed.
                Defaults to True.
        """
        cmd = ["mkfs.ext4", "-F"]

        if not fsuuid:
            try:
                fsuuid = cls.get_attrs_by_dev("UUID", dev)
                assert fsuuid
                logger.debug(f"reuse previous UUID: {fsuuid}")
            except Exception:
                pass
        if fsuuid:
            logger.debug(f"using UUID: {fsuuid}")
            cmd.extend(["-U", fsuuid])

        if not fslabel:
            try:
                fslabel = cls.get_attrs_by_dev("LABEL", dev)
                assert fslabel
                logger.debug(f"reuse previous fs LABEL: {fslabel}")
            except Exception:
                pass
        if fslabel:
            logger.debug(f"using fs LABEL: {fslabel}")
            cmd.extend(["-L", fslabel])

        cmd.append(dev)
        logger.warning(f"format {dev} to ext4: {cmd=}")
        subprocess_call(cmd, raise_exception=raise_exception)

    @classmethod
    def mount_ro(
        cls, *, target: str, mount_point: str | Path, raise_exception: bool = True
    ):
        """Mount <target> to <mount_point> read-only.

        If the target device is mounted, we bind mount the target device to mount_point.
        if the target device is not mounted, we directly mount it to the mount_point.

        Args:
            target (str): target to be mounted.
            mount_point (str | Path): mount point to mount to.
            raise_exception (bool, optional): raise exception on subprocess call failed.
                Defaults to True.
        """
        # NOTE: set raise_exception to false to allow not mounted
        #       not mounted dev will have empty return str
        if _active_mount_point := CMDHelperFuncs.get_mount_point_by_dev(
            target, raise_exception=False
        ):
            CMDHelperFuncs.bind_mount_ro(
                _active_mount_point,
                mount_point,
                raise_exception=raise_exception,
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
            subprocess_call(cmd, raise_exception=raise_exception)

    @classmethod
    def reboot(cls, args: Optional[list[str]] = None) -> NoReturn:
        """Reboot the system, with optional args passed to reboot command.

        This is implemented by calling:
            reboot [args[0], args[1], ...]

        NOTE(20230614): this command makes otaclient exit immediately.
        NOTE(20240421): rpi_boot's reboot takes args.

        Args:
            args (Optional[list[str]], optional): args passed to reboot command.
                Defaults to None, not passing any args.
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
        if _loaded_ota_status is None:
            logger.info(
                "ota_status files incompleted/not presented, "
                f"initializing and set/store status to {api_types.StatusOta.INITIALIZED.name}..."
            )
            self._store_current_status(api_types.StatusOta.INITIALIZED)
            self._ota_status = api_types.StatusOta.INITIALIZED
            return
        logger.info(f"status loaded from file: {_loaded_ota_status.name}")

        # status except UPDATING and ROLLBACKING(like SUCCESS/FAILURE/ROLLBACK_FAILURE)
        # are remained as it
        if _loaded_ota_status not in [
            api_types.StatusOta.UPDATING,
            api_types.StatusOta.ROLLBACKING,
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
                self._ota_status = api_types.StatusOta.SUCCESS
                self._store_current_status(api_types.StatusOta.SUCCESS)
            else:
                self._ota_status = (
                    api_types.StatusOta.ROLLBACK_FAILURE
                    if _loaded_ota_status == api_types.StatusOta.ROLLBACKING
                    else api_types.StatusOta.FAILURE
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
                api_types.StatusOta.ROLLBACK_FAILURE
                if _loaded_ota_status == api_types.StatusOta.ROLLBACKING
                else api_types.StatusOta.FAILURE
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
        write_str_to_file_atomic(
            self.current_ota_status_dir / cfg.SLOT_IN_USE_FNAME, _slot
        )

    def _store_standby_slot_in_use(self, _slot: str):
        write_str_to_file_atomic(
            self.standby_ota_status_dir / cfg.SLOT_IN_USE_FNAME, _slot
        )

    def _load_current_slot_in_use(self) -> Optional[str]:
        if res := read_str_from_file(
            self.current_ota_status_dir / cfg.SLOT_IN_USE_FNAME, _default=""
        ):
            return res

    # status control

    def _store_current_status(self, _status: api_types.StatusOta):
        write_str_to_file_atomic(
            self.current_ota_status_dir / cfg.OTA_STATUS_FNAME, _status.name
        )

    def _store_standby_status(self, _status: api_types.StatusOta):
        write_str_to_file_atomic(
            self.standby_ota_status_dir / cfg.OTA_STATUS_FNAME, _status.name
        )

    def _load_current_status(self) -> Optional[api_types.StatusOta]:
        if _status_str := read_str_from_file(
            self.current_ota_status_dir / cfg.OTA_STATUS_FNAME, _default=""
        ).upper():
            with contextlib.suppress(KeyError):
                # invalid status string
                return api_types.StatusOta[_status_str]

    # version control

    def _store_standby_version(self, _version: str):
        write_str_to_file_atomic(
            self.standby_ota_status_dir / cfg.OTA_VERSION_FNAME,
            _version,
        )

    # helper methods

    def _is_switching_boot(self, active_slot: str) -> bool:
        """Detect whether we should switch boot or not with ota_status files."""
        # evidence: ota_status
        _is_updating_or_rollbacking = self._load_current_status() in [
            api_types.StatusOta.UPDATING,
            api_types.StatusOta.ROLLBACKING,
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
        self._store_current_status(api_types.StatusOta.FAILURE)
        self._store_current_slot_in_use(self.standby_slot)

    def pre_update_standby(self, *, version: str):
        """On pre_update stage, set standby slot's status to UPDATING,
        set slot_in_use to standby slot, and set version.

        NOTE: expecting standby slot to be mounted and ready for use!
        """
        # create the ota-status folder unconditionally
        self.standby_ota_status_dir.mkdir(exist_ok=True, parents=True)
        # store status to standby slot
        self._store_standby_status(api_types.StatusOta.UPDATING)
        self._store_standby_version(version)
        self._store_standby_slot_in_use(self.standby_slot)

    def pre_rollback_current(self):
        self._store_current_status(api_types.StatusOta.FAILURE)

    def pre_rollback_standby(self):
        # store ROLLBACKING status to standby
        self.standby_ota_status_dir.mkdir(exist_ok=True, parents=True)
        self._store_standby_status(api_types.StatusOta.ROLLBACKING)

    def load_active_slot_version(self) -> str:
        return read_str_from_file(
            self.current_ota_status_dir / cfg.OTA_VERSION_FNAME,
            _default=cfg.DEFAULT_VERSION_STR,
        )

    def on_failure(self):
        """Store FAILURE to status file on failure."""
        self._store_current_status(api_types.StatusOta.FAILURE)
        # when standby slot is not created, otastatus is not needed to be set
        if self.standby_ota_status_dir.is_dir():
            self._store_standby_status(api_types.StatusOta.FAILURE)

    @property
    def booted_ota_status(self) -> api_types.StatusOta:
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

    def mount_standby(self) -> None:
        """Mount standby slot dev to <standby_slot_mount_point>."""
        logger.debug("mount standby slot rootfs dev...")
        if CMDHelperFuncs.is_target_mounted(
            self.standby_slot_dev, raise_exception=False
        ):
            logger.debug(f"{self.standby_slot_dev=} is mounted, try to umount it ...")
            CMDHelperFuncs.umount(self.standby_slot_dev, raise_exception=False)

        CMDHelperFuncs.mount_rw(
            target=self.standby_slot_dev,
            mount_point=self.standby_slot_mount_point,
        )

    def mount_active(self) -> None:
        """Mount active rootfs ready-only."""
        logger.debug("mount active slot rootfs dev...")
        CMDHelperFuncs.mount_ro(
            target=self.active_slot_dev,
            mount_point=self.active_slot_mount_point,
        )

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
            raise ValueError(
                f"failed to copy /boot/ota from active to standby: {e!r}"
            ) from e

    def prepare_standby_dev(
        self,
        *,
        erase_standby: bool = False,
        fslabel: Optional[str] = None,
    ) -> None:
        CMDHelperFuncs.umount(self.standby_slot_dev, raise_exception=False)
        if erase_standby:
            return CMDHelperFuncs.mkfs_ext4(self.standby_slot_dev, fslabel=fslabel)

        # TODO: in the future if in-place update mode is implemented, do a
        #   fschck over the standby slot file system.
        if fslabel:
            CMDHelperFuncs.set_ext4_fslabel(self.standby_slot_dev, fslabel=fslabel)

    def umount_all(self, *, ignore_error: bool = True):
        logger.debug("unmount standby slot and active slot mount point...")
        CMDHelperFuncs.umount(
            self.standby_slot_mount_point, raise_exception=ignore_error
        )
        CMDHelperFuncs.umount(
            self.active_slot_mount_point, raise_exception=ignore_error
        )


def cat_proc_cmdline(target: str = "/proc/cmdline") -> str:
    return read_str_from_file(target, _default="")
