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
"""Boot control implementation for NVIDIA Jetson device boot with cboot."""


from __future__ import annotations
import logging
import os
import re
from functools import partial
from pathlib import Path
from subprocess import run, CalledProcessError
from typing import Any, Generator, NamedTuple, Literal, Optional

from pydantic import BaseModel, BeforeValidator, PlainSerializer
from typing_extensions import Annotated, Self

from otaclient.app import errors as ota_errors
from otaclient.app.common import copytree_identical, write_str_to_file_sync
from otaclient.app.proto import wrapper
from ._common import (
    OTAStatusFilesControl,
    SlotMountHelper,
    CMDHelperFuncs,
)
from .configs import cboot_cfg as cfg
from .protocol import BootControllerProtocol

logger = logging.getLogger(__name__)


class SlotID(str):
    VALID_SLOTS = ["0", "1"]

    def __new__(cls, _in: str | Self) -> Self:
        if isinstance(_in, cls):
            return _in
        if _in in cls.VALID_SLOTS:
            return str.__new__(cls, _in)
        raise ValueError(f"{_in=} is not valid slot num, should be '0' or '1'.")


class BSPVersion(NamedTuple):
    """
    example: 32.6.1
    see https://developer.nvidia.com/embedded/jetson-linux-archive for BSP version history.
    """

    major_ver: int
    major_rev: int
    minor_rev: int

    @classmethod
    def parse(cls, _in: str) -> Self:
        """Parse "Rxx.yy.z string into BSPVersion."""
        major_ver, major_rev, minor_rev = _in[1:].split(".")
        return cls(
            int(major_ver),
            int(major_rev),
            int(minor_rev),
        )

    @staticmethod
    def dump(to_export: BSPVersion) -> str:
        """Dump BSPVersion to string as "Rxx.yy.z"."""
        return f"R{to_export.major_ver}.{to_export.major_rev}.{to_export.minor_rev}"


BSPVersionStr = Annotated[
    BSPVersion,
    BeforeValidator(BSPVersion.parse),
    PlainSerializer(BSPVersion.dump, return_type=str),
]


class FirmwareBSPVersion(BaseModel):
    """
    BSP version string schema: Rxx.yy.z
    """

    slot_a: Optional[BSPVersionStr] = None
    slot_b: Optional[BSPVersionStr] = None


class JetsonCBootContrlError(Exception):
    """Exception types for covering jetson-cboot related errors."""


class _NVBootctrl:
    """Helper for calling nvbootctrl commands."""

    NVBOOTCTRL = "nvbootctrl"
    NVBootctrlTarget = Literal["bootloader", "rootfs"]
    slot_id_flip: dict[SlotID, SlotID] = {
        SlotID("0"): SlotID("1"),
        SlotID("1"): SlotID("0"),
    }

    @classmethod
    def _nvbootctrl(
        cls,
        _cmd: str,
        _slot_id: Optional[SlotID] = None,
        *,
        check_output=False,
        target: Optional[NVBootctrlTarget] = None,
    ) -> Any:
        cmd = [cls.NVBOOTCTRL]
        if target:
            cmd.extend(["-t", target])
        cmd.append(_cmd)
        if _slot_id:
            cmd.append(str(_slot_id))

        res = run(
            cmd,
            check=True,
            capture_output=True,
        )
        if check_output:
            return res.stdout.decode()
        return

    @staticmethod
    def _check_slot_id(slot_id: SlotID) -> SlotID:
        if slot_id not in [SlotID("0"), SlotID("1")]:
            raise ValueError(f"invalid slot id: {slot_id=}")
        return slot_id

    @classmethod
    def get_current_slot(cls, *, target: Optional[NVBootctrlTarget] = None) -> SlotID:
        """Prints currently running SLOT."""
        cmd = "get-current-slot"
        res = cls._nvbootctrl(cmd, check_output=True, target=target)
        assert isinstance(res, str), f"invalid output from get-current-slot: {res}"
        return SlotID(res.strip())

    @classmethod
    def get_standby_slot(cls, *, target: Optional[NVBootctrlTarget] = None) -> SlotID:
        """Prints standby SLOT.

        NOTE: this method is implemented by nvbootctrl get-current-slot.
        """
        return cls.slot_id_flip[cls.get_current_slot(target=target)]

    @classmethod
    def set_active_boot_slot(
        cls, slot_id: SlotID, *, target: Optional[NVBootctrlTarget] = None
    ) -> None:
        """On next boot, load and execute SLOT."""
        cmd = "set-active-boot-slot"
        return cls._nvbootctrl(cmd, cls._check_slot_id(slot_id), target=target)

    @classmethod
    def set_slot_as_unbootable(
        cls, slot_id: SlotID, *, target: Optional[NVBootctrlTarget] = None
    ) -> None:
        """Mark SLOT as invalid."""
        cmd = "set-slot-as-unbootable"
        return cls._nvbootctrl(cmd, cls._check_slot_id(slot_id), target=target)

    @classmethod
    def dump_slots_info(cls, *, target: Optional[NVBootctrlTarget] = None) -> str:
        """Prints info for slots."""
        cmd = "dump-slots-info"
        return cls._nvbootctrl(cmd, target=target, check_output=True)

    @classmethod
    def is_unified_enabled(cls) -> bool | None:
        """Returns 0 only if unified a/b is enabled.

        NOTE: this command is available after BSP R32.6.1.

        Meaning of return code:
            - 0 if both unified A/B and rootfs A/B are enabled
            - 69 if both unified A/B and rootfs A/B are disabled
            - 70 if rootfs A/B is enabled and unified A/B is disabled

        Returns:
            True for both unified A/B and rootfs A/B are enbaled,
                False for unified A/B disabled but rootfs A/B enabled,
                None for both disabled.
        """
        cmd = "is-unified-enabled"
        try:
            cls._nvbootctrl(cmd)
            return True
        except CalledProcessError as e:
            if e.returncode == 70:
                return False
            elif e.returncode == 69:
                return
            raise ValueError(f"{cmd} returns unexpected result: {e.returncode=}, {e!r}")


class NVUpdateEngine:
    """Firmware update implementation using nv_update_engine."""

    NV_UPDATE_ENGINE = "nv_update_engine"

    @classmethod
    def _nv_update_engine(cls, payload: Path | str):
        cmd = [
            cls.NV_UPDATE_ENGINE,
            "-i",
            "bl",
            "--payload",
            str(payload),
            "--no-reboot",
        ]
        logger.info(f"apply BUP {payload=}")
        run(cmd, check=True, capture_output=True)

    @classmethod
    def _nv_update_engine_unified_ab(cls, payload: Path | str):
        cmd = [
            cls.NV_UPDATE_ENGINE,
            "-i",
            "bl-only",
            "--payload",
            str(payload),
        ]
        logger.info(f"apply BUP {payload=} with unified A/B")
        run(cmd, check=True, capture_output=True)

    @classmethod
    def apply_firmware_update(cls, payload: Path | str, *, unified_ab: bool) -> None:
        """Apply firmware update."""
        if unified_ab:
            return cls._nv_update_engine_unified_ab(payload)
        cls._nv_update_engine(payload)

    @classmethod
    def verify_update(cls) -> str:
        """Dump the nv_update_engine update verification."""
        cmd = [cls.NV_UPDATE_ENGINE, "--verify"]

class FirmwareBSPVersionControl:
    """firmware_bsp_version ota-status file for tracking firmware version."""

    def __init__(
        self, current_firmware_bsp_vf: Path, standby_firmware_bsp_vf: Path
    ) -> None:
        self._version = FirmwareBSPVersion()
        self._current_fw_bsp_vf = current_firmware_bsp_vf
        self._standby_fw_bsp_vf = standby_firmware_bsp_vf

    def read_firmware_bsp_version(self):
        """Ready firmware BSP version from current slot's fw_bsp_version file."""
        try:
            self._version = FirmwareBSPVersion.model_validate_json(
                self._current_fw_bsp_vf.read_text()
            )
        except Exception as e:
            logger.warning(
                f"invalid or missing firmware_bsp_verion file, removed: {e!r}"
            )
            self._current_fw_bsp_vf.unlink(missing_ok=True)

    def write_current_firmware_bsp_version(self) -> None:
        """Write instance firmware_bsp_version to firmware_bsp_version file."""
        write_str_to_file_sync(self._current_fw_bsp_vf, self._version.model_dump_json())

    def write_standby_firmware_bsp_version(self) -> None:
        """Write instance firmware_bsp_version to firmware_bsp_version file."""
        write_str_to_file_sync(self._standby_fw_bsp_vf, self._version.model_dump_json())

    def get_version_by_slot(self, slot_id: SlotID) -> Optional[BSPVersion]:
        if slot_id == "0":
            return self._version.slot_a
        elif slot_id == "1":
            return self._version.slot_b
        raise ValueError(f"invalid {slot_id=}")

    def set_version_by_slot(self, slot_id: SlotID, version: Optional[BSPVersion]):
        if slot_id == "0":
            self._version.slot_a = version
        elif slot_id == "1":
            self._version.slot_b = version
        else:
            raise ValueError(f"invalid {slot_id=}")


BSP_VER_PA = re.compile(
    (
        r"# R(?P<major_ver>\d+) \(\w+\), REVISION: (?P<major_rev>\d+)\.(?P<minor_rev>\d+), "
        r"GCID: (?P<gcid>\d+), BOARD: (?P<board>\w+), EABI: (?P<eabi>\w+)"
    )
)


def parse_bsp_version(nv_tegra_release: str) -> BSPVersion:
    """Get current BSP version from contents of /etc/nv_tegra_release.

    Example: # R32 (release), REVISION: 6.1, GCID: 27863751, BOARD: t186ref, EABI: aarch64, DATE: Mon Jul 26 19:36:31 UTC 2021
    """
    ma = BSP_VER_PA.match(nv_tegra_release)
    assert ma, f"invalid nv_tegra_release content: {nv_tegra_release}"
    return BSPVersion(
        int(ma.group("major_ver")),
        int(ma.group("major_rev")),
        int(ma.group("minor_rev")),
    )


class _CBootControl:

    MMCBLK_DEV_PREFIX = "mmcblk"  # internal emmc
    NVMESSD_DEV_PREFIX = "nvme"  # external nvme ssd
    INTERNAL_EMMC_DEVNAME = "mmcblk0"
    _slot_id_partid = {SlotID("0"): "1", SlotID("1"): "2"}

    def __init__(self):
        # ------ sanity check, confirm we are at jetson device ------ #
        if not os.path.exists(cfg.TEGRA_CHIP_ID_PATH):
            _err_msg = f"not a jetson device, {cfg.TEGRA_CHIP_ID_PATH} doesn't exist"
            logger.error(_err_msg)
            raise JetsonCBootContrlError(_err_msg)

        # ------ check BSP version ------ #
        try:
            self.bsp_version = bsp_version = parse_bsp_version(
                Path(cfg.NV_TEGRA_RELEASE_FPATH).read_text()
            )
        except Exception as e:
            _err_msg = f"failed to detect BSP version: {e!r}"
            logger.error(_err_msg)
            raise JetsonCBootContrlError(_err_msg)
        logger.info(f"{bsp_version=}")

        # ------ sanity check, jetson-cboot is not used after BSP R34 ------ #
        if not bsp_version < (34, 0, 0):
            _err_msg = (
                f"jetson-cboot only supports BSP version < R34, but get {bsp_version=}. "
                "Please use jetson-uefi bootloader type for this device."
            )
            logger.error(_err_msg)
            raise JetsonCBootContrlError(_err_msg)

        # ------ check if unified A/B is enabled ------ #
        self.unified_ab_enabled = unified_ab_enabled = False
        if bsp_version >= (32, 6, 0):
            # NOTE: unified A/B is supported starting from r32.6
            self.unified_ab_enabled = unified_ab_enabled = (
                _NVBootctrl.is_unified_enabled()
            )
            if unified_ab_enabled is None:
                _err_msg = "rootfs A/B is not enabled!"
                logger.error(_err_msg)
                raise JetsonCBootContrlError(_err_msg)
        else:
            try:
                _NVBootctrl.get_current_slot()
            except CalledProcessError:
                _err_msg = "rootfs A/B is not enabled!"
                logger.error(_err_msg)
                raise JetsonCBootContrlError(_err_msg)

        if unified_ab_enabled:
            logger.info("unified A/B is enabled")

        # ------ check A/B slots ------ #
        self.current_bootloader_slot = current_bootloader_slot = (
            _NVBootctrl.get_current_slot()
        )
        self.standby_bootloader_slot = standby_bootloader_slot = (
            _NVBootctrl.get_standby_slot()
        )
        if not unified_ab_enabled:
            self.current_rootfs_slot = current_rootfs_slot = (
                _NVBootctrl.get_current_slot(target="rootfs")
            )
            self.standby_rootfs_slot = standby_rootfs_slot = (
                _NVBootctrl.get_standby_slot(target="rootfs")
            )
        else:
            self.current_rootfs_slot = current_rootfs_slot = current_bootloader_slot
            self.standby_rootfs_slot = standby_rootfs_slot = standby_bootloader_slot

        # check if rootfs slot and bootloader slot mismatches, this only happens
        #   when unified_ab is not enabled.
        if current_rootfs_slot != current_bootloader_slot:
            logger.warning(
                "bootloader and rootfs A/B slot mismatches: "
                f"{current_rootfs_slot=} != {current_bootloader_slot=}"
            )
            logger.warning("this might indicates a failed previous firmware update")

        # ------ detect rootfs_dev and parent_dev ------ #
        self.cuurent_rootfs_devpath = current_rootfs_devpath = (
            CMDHelperFuncs.get_current_rootfs_dev().strip()
        )
        self.parent_devpath = parent_devpath = Path(
            CMDHelperFuncs.get_parent_dev(current_rootfs_devpath)
        )

        self.external_rootfs = False
        parent_devname = parent_devpath.name
        if parent_devname.startswith(self.MMCBLK_DEV_PREFIX):
            logger.info(f"device boots from internal emmc: {parent_devpath}")
        elif parent_devname.startswith(self.NVMESSD_DEV_PREFIX):
            logger.info(f"device boots from external nvme ssd: {parent_devpath}")
            self.external_rootfs = True
        else:
            _err_msg = f"we don't support boot from {parent_devpath=} currently"
            logger.error(_err_msg)
            raise JetsonCBootContrlError(_err_msg) from NotImplementedError(
                f"unsupported bootdev {parent_devpath}"
            )

        # rootfs partition
        self.standby_rootfs_devpath = (
            f"/dev/{parent_devname}p{self._slot_id_partid[standby_rootfs_slot]}"
        )
        self.standby_rootfs_dev_partuuid = CMDHelperFuncs.get_partuuid_by_dev(
            f"{self.standby_rootfs_devpath}"
        )
        logger.info(
            "finish detecting rootfs devs: \n"
            f"active_slot({current_rootfs_slot}): {self.cuurent_rootfs_devpath=}\n"
            f"standby_slot({standby_rootfs_slot}): {self.standby_rootfs_devpath=}, {self.standby_rootfs_dev_partuuid=}"
        )

        # internal emmc partition
        self.standby_internal_emmc_devpath = f"/dev/{self.INTERNAL_EMMC_DEVNAME}p{self._slot_id_partid[standby_rootfs_slot]}"

        logger.info(f"finished cboot control init: {current_rootfs_slot=}")
        logger.info(f"nvbootctrl dump-slots-info: \n{_NVBootctrl.dump_slots_info()}")
        if not unified_ab_enabled:
            logger.info(
                f"nvbootctrl -t rootfs dump-slots-info: \n{_NVBootctrl.dump_slots_info(target='rootfs')}"
            )

    # API

    @property
    def external_rootfs_enabled(self) -> bool:
        return self.external_rootfs

    def finalize_switching_boot(self) -> bool:
        """Dump information after OTA reboot, this method always return True.

        Actually we don't need to do anything for finalizing jetson-cboot, as:

            1. if rootfs/bootloader boots failed, jetson boot will automatically
                fallback to previous slot. This situation can be handled by OTAStatusFilesControl.

            2. if boot switches successfully, the jetson boot will automatically
                set the status of slots to success.
        """
        try:
            logger.info(f"nv_update_engine verify: \n{NVUpdateEngine.verify_update()}")
        except CalledProcessError as e:
            logger.warning(f"failed to dump info: {e!r}")
        return True

    def set_standby_rootfs_unbootable(self):
        _NVBootctrl.set_slot_as_unbootable(self.standby_rootfs_slot, target="rootfs")

    def switch_boot(self) -> None:
        # NOTE(20240412): we always try to align bootloader slot with rootfs.
        target_slot = self.standby_rootfs_slot

        logger.info(f"switch boot to {target_slot=}")
        if not self.unified_ab_enabled:
            _NVBootctrl.set_active_boot_slot(target_slot, target="rootfs")

        # when unified_ab enabled, switching bootloader slot will also switch
        #   the rootfs slot.
        _NVBootctrl.set_active_boot_slot(target_slot)

    def prepare_standby_dev(self, *, erase_standby: bool):
        if CMDHelperFuncs.is_target_mounted(self.standby_rootfs_devpath):
            CMDHelperFuncs.umount(self.standby_rootfs_devpath)

        if erase_standby:
            try:
                CMDHelperFuncs.mkfs_ext4(self.standby_rootfs_devpath)
            except Exception as e:
                _err_msg = f"failed to mkfs.ext4 on standby dev: {e!r}"
                logger.error(_err_msg)
                raise JetsonCBootContrlError(_err_msg) from e
        # TODO: in the future if in-place update mode is implemented, do a
        #   fschck over the standby slot file system.

    @staticmethod
    def update_extlinux_cfg(_input: str, partuuid: str) -> str:
        """Update input exlinux text with input rootfs <partuuid_str>."""

        partuuid_str = f"PARTUUID={partuuid}"

        def _replace(ma: re.Match, repl: str):
            append_l: str = ma.group(0)
            if append_l.startswith("#"):
                return append_l
            res, n = re.compile(r"root=[\w\-=]*").subn(repl, append_l)
            if not n:  # this APPEND line doesn't contain root= placeholder
                res = f"{append_l} {repl}"

            return res

        _repl_func = partial(_replace, repl=f"root={partuuid_str}")
        return re.compile(r"\n\s*APPEND.*").sub(_repl_func, _input)


class JetsonCBootControl(BootControllerProtocol):
    """BootControllerProtocol implementation for jetson-cboot."""

    FIRMWARE_BSA_VERSION_FNAME = "firmware_bsp_version"

    def __init__(self) -> None:
        try:
            self._cboot_control = _CBootControl()

            # mount point prepare
            self._mp_control = SlotMountHelper(
                standby_slot_dev=self._cboot_control.standby_rootfs_devpath,
                standby_slot_mount_point=cfg.MOUNT_POINT,
                active_slot_dev=self._cboot_control.cuurent_rootfs_devpath,
                active_slot_mount_point=cfg.ACTIVE_ROOT_MOUNT_POINT,
            )
            # init ota-status files
            self._ota_status_control = OTAStatusFilesControl(
                active_slot=str(self._cboot_control.current_rootfs_slot),
                standby_slot=str(self._cboot_control.standby_rootfs_slot),
                current_ota_status_dir=Path(cfg.ACTIVE_ROOTFS_PATH)
                / Path(cfg.OTA_STATUS_DIR).relative_to("/"),
                # NOTE: might not yet be populated before OTA update applied!
                standby_ota_status_dir=Path(cfg.MOUNT_POINT)
                / Path(cfg.OTA_STATUS_DIR).relative_to("/"),
                finalize_switching_boot=self._cboot_control.finalize_switching_boot,
            )

            # read firmware_bsp_verion from current slot
            self.firmware_bsp_version = FirmwareBSPVersion()

            fw_bsp_vf = (
                self._ota_status_control.current_ota_status_dir
                / self.FIRMWARE_BSA_VERSION_FNAME
            )
            try:
                self.firmware_bsp_version = FirmwareBSPVersion.model_validate_json(
                    fw_bsp_vf.read_text()
                )
            except Exception as e:
                logger.warning(
                    f"invalid or missing firmware_bsp_verion file, removed: {e!r}"
                )
                fw_bsp_vf.unlink(missing_ok=True)

        except Exception as e:
            _err_msg = f"failed to start jetson-cboot controller: {e!r}"
            raise ota_errors.BootControlStartupFailed(_err_msg, module=__name__) from e

    def _copy_standby_slot_boot_to_internal_emmc(self):
        """Copy the standby slot's /boot to internal emmc dev.

        This method is involved when external rootfs is enabled, aligning with
            the behavior with the NVIDIA flashing script.

        NOTE: at the time this method is called, the /boot folder at
            standby slot rootfs MUST be fully setup!
        """
        # mount the actual standby_boot_dev now
        _internal_emmc_mp = Path(cfg.SEPARATE_BOOT_MOUNT_POINT)
        _internal_emmc_mp.mkdir(exist_ok=True, parents=True)

        try:
            CMDHelperFuncs.mount_rw(
                self._cboot_control.standby_internal_emmc_devpath,
                _internal_emmc_mp,
            )
        except Exception as e:
            _msg = f"failed to mount standby internal emmc dev: {e!r}"
            logger.error(_msg)
            raise JetsonCBootContrlError(_msg) from e

        try:
            dst = _internal_emmc_mp / "boot"
            dst.mkdir(exist_ok=True, parents=True)
            src = self._mp_control.standby_slot_mount_point / "boot"

            # copy the standby slot's boot folder to emmc boot dev
            copytree_identical(src, dst)
        except Exception as e:
            _msg = f"failed to populate standby slot's /boot folder to standby internal emmc dev: {e!r}"
            logger.error(_msg)
            raise JetsonCBootContrlError(_msg) from e
        finally:
            CMDHelperFuncs.umount(_internal_emmc_mp, ignore_error=True)

    def _preserve_ota_config_files_to_standby(self):
        """Preserve /boot/ota to standby /boot folder."""
        src = self._mp_control.active_slot_mount_point / "boot" / "ota"
        if not src.is_dir():  # basically it is not possible
            logger.info(f"{src} doesn't exist, skip preserve /boot/ota folder.")
            return

        dst = self._mp_control.standby_slot_mount_point / "boot" / "ota"
        # TODO: (20240411) reconsidering should we preserve /boot/ota?
        copytree_identical(src, dst)

    def _update_standby_slot_extlinux_cfg(self):
        src = standby_slot_extlinux = self._mp_control.standby_slot_mount_point / Path(
            cfg.EXTLINUX_FILE
        ).relative_to("/")
        if not standby_slot_extlinux.is_file():
            src = self._mp_control.active_slot_mount_point / Path(
                cfg.EXTLINUX_FILE
            ).relative_to("/")

        # update the extlinux.conf with standby slot rootfs' partuuid
        updated_extlinux_cfg = self._cboot_control.update_extlinux_cfg(
            src.read_text(),
            self._cboot_control.standby_rootfs_dev_partuuid,
        )
        write_str_to_file_sync(standby_slot_extlinux, updated_extlinux_cfg)

    def _nv_firmware_update(self) -> Optional[bool]:
        """Perform firmware update with nv_update_engine.

        NOTE(20240412): mostly a failed firmware update is caused by BUP not applicable
            to the device, and no changes will be applied in this condidition.

        Returns:
            True if firmware update applied, False for failed firmware update,
                None for no firmware update occurs.
        """
        logger.info("jetson-cboot: entering nv firmware update ...")
        standby_bootloader_slot = self._cboot_control.standby_bootloader_slot
        standby_firmware_bsp_ver = self.firmware_bsp_version.get_bsp_ver_by_slot(
            standby_bootloader_slot
        )
        logger.info(f"{standby_bootloader_slot=} BSP ver: {standby_firmware_bsp_ver}")

        # ------ check if we need to do firmware update ------ #
        _new_bsp_v_fpath = self._mp_control.standby_slot_mount_point / Path(
            cfg.NV_TEGRA_RELEASE_FPATH
        ).relative_to("/")
        try:
            new_bsp_v = parse_bsp_version(_new_bsp_v_fpath.read_text())
        except Exception as e:
            logger.warning(f"failed to detect new image's BSP version: {e!r}")
            logger.info("skip firmware update due to new image BSP version unknown")
            return

        logger.info(f"BUP package version: {new_bsp_v=}")
        if standby_firmware_bsp_ver and standby_firmware_bsp_ver >= new_bsp_v:
            logger.info(
                f"{standby_bootloader_slot=} has newer or equal ver of firmware, skip firmware update"
            )
            return

        # ------ preform firmware update ------ #
        # TODO: /opt/ota_package config and firmware configs
        firmware_dpath = self._mp_control.standby_slot_mount_point / Path(
            cfg.FIRMWARE_DPATH
        ).relative_to("/")

        _firmware_applied = False
        for firmware in cfg.FIRMWARE_LIST:
            if (firmware_fpath := firmware_dpath / firmware).is_file():
                logger.info(f"nv_firmware: apply {firmware_fpath} ...")
                try:
                    NVUpdateEngine.apply_firmware_update(
                        firmware_fpath,
                        unified_ab=bool(self._cboot_control.unified_ab_enabled),
                    )
                    _firmware_applied = True
                except CalledProcessError as e:
                    _err_msg = f"failed to apply BUP {firmware_fpath}: {e!r}, {e.stderr=}, {e.stdout=}"
                    logger.error(_err_msg)
                    logger.warning("firmware update interrupted")
                    return False

        # ------ register new firmware version ------ #
        if _firmware_applied:
            logger.info(
                f"nv_firmware: successfully apply firmware to {self._cboot_control.standby_rootfs_slot=}"
            )
            self.firmware_bsp_version.set_bsp_ver_by_slot(
                standby_bootloader_slot, new_bsp_v
            )
            return True
        logger.info("no firmware payload BUP available, skip firmware update")

    def _write_firmware_bsp_version(self) -> None:
        """Write instance firmware_bsp_version to firmware_bsp_version file."""
        _fw_bsp_v_json = self.firmware_bsp_version.model_dump_json()
        current_version_file = (
            self._ota_status_control.current_ota_status_dir
            / self.FIRMWARE_BSA_VERSION_FNAME
        )
        standby_version_file = (
            self._ota_status_control.standby_ota_status_dir
            / self.FIRMWARE_BSA_VERSION_FNAME
        )
        write_str_to_file_sync(standby_version_file, _fw_bsp_v_json)
        write_str_to_file_sync(current_version_file, _fw_bsp_v_json)

    # APIs

    def get_standby_slot_path(self) -> Path:
        return self._mp_control.standby_slot_mount_point

    def get_standby_boot_dir(self) -> Path:
        return self._mp_control.standby_boot_dir

    def pre_update(self, version: str, *, standby_as_ref: bool, erase_standby: bool):
        try:
            logger.info("jetson-cboot: pre-update ...")
            # udpate active slot's ota_status
            self._ota_status_control.pre_update_current()

            if not self._cboot_control.unified_ab_enabled:
                # set standby rootfs as unbootable as we are going to update it
                self._cboot_control.set_standby_rootfs_unbootable()

            # prepare standby slot dev
            self._cboot_control.prepare_standby_dev(erase_standby=erase_standby)
            # mount slots
            self._mp_control.mount_standby()
            self._mp_control.mount_active()

            # update standby slot's ota_status files
            self._ota_status_control.pre_update_standby(version=version)
        except Exception as e:
            _err_msg = f"failed on pre_update: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPreUpdateFailed(
                _err_msg, module=__name__
            ) from e

    def post_update(self) -> Generator[None, None, None]:
        try:
            logger.info("jetson-cboot: post-update ...")
            # ------ update extlinux.conf ------ #
            self._update_standby_slot_extlinux_cfg()

            # ------ firmware update ------ #
            if self._nv_firmware_update():
                # firmware updated, update the firmware_bsp_version file
                self._write_firmware_bsp_version()

            # ------ preserve /boot/ota folder to standby rootfs ------ #
            self._preserve_ota_config_files_to_standby()

            # ------ for external rootfs, preserve /boot folder to internal ------ #
            if self._cboot_control.external_rootfs:
                logger.info(
                    "rootfs on external storage detected: "
                    "copy standby slot rootfs' /boot folder "
                    "to corresponding internal emmc dev ..."
                )
                self._copy_standby_slot_boot_to_internal_emmc()

            # ------ switch boot to standby ------ #
            self._cboot_control.switch_boot()

            # ------ prepare to reboot ------ #
            self._mp_control.umount_all(ignore_error=True)
            logger.info(f"[post-update]: {_NVBootctrl.dump_slots_info()=}")

            logger.info("post update finished, wait for reboot ...")
            yield  # hand over control back to otaclient
            CMDHelperFuncs.reboot()
        except Exception as e:
            _err_msg = f"failed on post_update: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPostUpdateFailed(
                _err_msg, module=__name__
            ) from e

    def pre_rollback(self):
        try:
            logger.info("jetson-cboot: pre-rollback setup ...")
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
            logger.info("jetson-cboot: post-rollback setup...")
            self._mp_control.umount_all(ignore_error=True)
            self._cboot_control.switch_boot()
            CMDHelperFuncs.reboot()
        except Exception as e:
            _err_msg = f"failed on post_rollback: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPostRollbackFailed(
                _err_msg, module=__name__
            ) from e

    def on_operation_failure(self):
        """Failure registering and cleanup at failure."""
        logger.warning("on failure try to unmounting standby slot...")
        self._ota_status_control.on_failure()
        self._mp_control.umount_all(ignore_error=True)

    def load_version(self) -> str:
        return self._ota_status_control.load_active_slot_version()

    def get_booted_ota_status(self) -> wrapper.StatusOta:
        return self._ota_status_control.booted_ota_status
