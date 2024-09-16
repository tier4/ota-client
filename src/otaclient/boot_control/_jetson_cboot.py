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
"""Boot control implementation for NVIDIA Jetson device boots with cboot.

Supports BSP version < R34.
"""


from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Generator, Optional

from otaclient._types import OTAStatus
from otaclient.app import errors as ota_errors
from otaclient.app.configs import config as cfg
from otaclient.boot_control._firmware_package import (
    FirmwareManifest,
    FirmwareUpdateRequest,
    PayloadType,
    load_firmware_package,
)
from otaclient_common import replace_root
from otaclient_common.common import file_digest, subprocess_run_wrapper
from otaclient_common.typing import StrOrPath

from ._common import CMDHelperFuncs, OTAStatusFilesControl, SlotMountHelper
from ._jetson_common import (
    SLOT_PAR_MAP,
    BSPVersion,
    FirmwareBSPVersionControl,
    NVBootctrlCommon,
    NVBootctrlExecError,
    NVBootctrlTarget,
    SlotID,
    copy_standby_slot_boot_to_internal_emmc,
    detect_external_rootdev,
    detect_rootfs_bsp_version,
    get_nvbootctrl_conf_tnspec,
    get_partition_devpath,
    preserve_ota_config_files_to_standby,
    update_standby_slot_extlinux_cfg,
)
from .configs import cboot_cfg as boot_cfg
from .protocol import BootControllerProtocol

logger = logging.getLogger(__name__)

MAXIMUM_SUPPORTED_BSP_VERSION_EXCLUDE = BSPVersion(34, 0, 0)
"""After R34, cboot is replaced by UEFI.

Also, cboot firmware update with nvupdate engine is supported up to this version(exclude).
"""


class JetsonCBootContrlError(Exception):
    """Exception types for covering jetson-cboot related errors."""


class NVBootctrlJetsonCBOOT(NVBootctrlCommon):
    """Helper for calling nvbootctrl commands.

    For BSP version < R34.
    Without -t option, the target will be bootloader by default.
    """

    @classmethod
    def mark_boot_successful(
        cls, slot_id: SlotID, *, target: Optional[NVBootctrlTarget] = None
    ) -> None:  # pragma: no cover
        """Mark current slot as GOOD."""
        cmd = "mark-boot-successful"
        try:
            cls._nvbootctrl(cmd, slot_id, check_output=False, target=target)
        except subprocess.CalledProcessError as e:
            logger.warning(f"nvbootctrl {cmd} call failed: {e!r}")

    @classmethod
    def set_slot_as_unbootable(
        cls, slot_id: SlotID, *, target: Optional[NVBootctrlTarget] = None
    ) -> None:  # pragma: no cover
        """Mark SLOT as invalid."""
        cmd = "set-slot-as-unbootable"
        try:
            return cls._nvbootctrl(
                cmd, SlotID(slot_id), check_output=False, target=target
            )
        except subprocess.CalledProcessError as e:
            logger.warning(f"nvbootctrl {cmd} call failed: {e!r}")

    @classmethod
    def is_unified_enabled(cls) -> bool | None:  # pragma: no cover
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
            cls._nvbootctrl(cmd, check_output=False)
            return True
        except subprocess.CalledProcessError as e:
            if e.returncode == 70:
                return False
            if e.returncode == 69:
                return
            logger.warning(f"{cmd} returns unexpected result: {e.returncode=}, {e!r}")


class NVUpdateEngine:
    """Firmware update implementation using nv_update_engine."""

    NV_UPDATE_ENGINE = "nv_update_engine"

    @classmethod
    def _nv_update_engine(cls, payload: Path | str):
        """nv_update_engine apply BUP, non unified_ab version."""
        # fmt: off
        cmd = [
            cls.NV_UPDATE_ENGINE,
            "-i", "bl",
            "--payload", str(payload),
            "--no-reboot",
        ]
        # fmt: on
        res = subprocess_run_wrapper(cmd, check=True, check_output=True)
        logger.info(
            (
                f"apply BUP {payload=}: \n"
                f"stdout: {res.stdout.decode()}\n"
                f"stderr: {res.stderr.decode()}"
            )
        )

    @classmethod
    def _nv_update_engine_unified_ab(cls, payload: Path | str):
        """nv_update_engine apply BUP, unified_ab version."""
        # fmt: off
        cmd = [
            cls.NV_UPDATE_ENGINE,
            "-i", "bl-only",
            "--payload", str(payload),
        ]
        # fmt: on
        res = subprocess_run_wrapper(cmd, check=True, check_output=True)
        logger.info(
            (
                f"apply BUP {payload=} with unified A/B: \n"
                f"stdout: {res.stdout.decode()}\n"
                f"stderr: {res.stderr.decode()}"
            )
        )

    def __init__(
        self,
        standby_slot_mp: StrOrPath,
        *,
        tnspec: str,
        firmware_update_request: FirmwareUpdateRequest,
        firmware_manifest: FirmwareManifest,
        unify_ab: bool,
    ) -> None:
        self._standby_slot_mp = standby_slot_mp
        self._tnspec = tnspec
        self._firmware_update_request = firmware_update_request
        self._firmware_manifest = firmware_manifest
        self._unify_ab = unify_ab

    def firmware_update(self) -> bool:
        """Perform firmware update if needed.

        Returns:
            True if firmware update is performed, False if there is no firmware update.
        """
        firmware_bsp_ver = BSPVersion.parse(
            self._firmware_manifest.firmware_spec.bsp_version
        )
        if firmware_bsp_ver >= MAXIMUM_SUPPORTED_BSP_VERSION_EXCLUDE:
            logger.warning(
                f"firmware package has {firmware_bsp_ver}, "
                f"which newer or equal to {MAXIMUM_SUPPORTED_BSP_VERSION_EXCLUDE}. "
                f"firmware update to {MAXIMUM_SUPPORTED_BSP_VERSION_EXCLUDE} or newer is NOT supported, "
                "skip firmware update"
            )
            return False

        # check firmware compatibility, this is to prevent failed firmware update beforehand.
        if not self._firmware_manifest.check_compat(self._tnspec):
            _err_msg = (
                "firmware package is incompatible with this device: "
                f"{self._tnspec=}, {self._firmware_manifest.firmware_spec.firmware_compat}, "
                "skip firmware update"
            )
            logger.warning(_err_msg)
            return False

        update_execute_func = (
            self._nv_update_engine_unified_ab
            if self._unify_ab
            else self._nv_update_engine
        )

        firmware_update_executed = False
        for update_payload in self._firmware_manifest.get_firmware_packages(
            self._firmware_update_request
        ):
            if update_payload.type != PayloadType.BUP:
                continue

            # NOTE: currently we only support payload indicated by file path.
            bup_flocation = update_payload.file_location
            bup_fpath = bup_flocation.location_path
            assert bup_flocation.location_type == "file" and isinstance(bup_fpath, str)

            payload_digest_alg, payload_digest_value = (
                update_payload.digest.algorithm,
                update_payload.digest.digest,
            )

            # bup is located at the OTA image
            bup_fpath = replace_root(
                bup_fpath,
                "/",
                self._standby_slot_mp,
            )
            if not Path(bup_fpath).is_file():
                logger.warning(f"{bup_fpath=} doesn't exist! skip...")
                continue

            _digest = file_digest(bup_fpath, algorithm=payload_digest_alg)
            if _digest != payload_digest_value:
                logger.warning(
                    f"{payload_digest_alg} validation failed, "
                    f"expect {payload_digest_value}, get {_digest}, "
                    f"skip apply {update_payload.payload_name}"
                )
                continue

            logger.warning(f"apply BUP {bup_fpath} to standby slot ...")
            update_execute_func(bup_fpath)
            firmware_update_executed = True
        return firmware_update_executed

    @classmethod
    def verify_update(cls) -> subprocess.CompletedProcess[bytes]:
        """Dump the nv_update_engine update verification.

        NOTE: no exception will be raised, the caller MUST check the
            call result by themselves.

        Returns:
            A CompletedProcess object with the call result.
        """
        cmd = [cls.NV_UPDATE_ENGINE, "--verify"]
        return subprocess_run_wrapper(cmd, check=False, check_output=True)


class _CBootControl:
    def __init__(self):
        # ------ check BSP version ------ #
        # NOTE(20240821): unfortunately, we don't have proper method to detect
        #   the firmware BSP version < R34, so we assume that the rootfs BSP version is the
        #   same as the firmware BSP version.
        try:
            self.rootfs_bsp_version = rootfs_bsp_version = detect_rootfs_bsp_version(
                rootfs=cfg.ACTIVE_ROOTFS_PATH
            )
        except Exception as e:
            _err_msg = f"failed to detect BSP version: {e!r}"
            logger.error(_err_msg)
            raise JetsonCBootContrlError(_err_msg) from None
        logger.info(f"{rootfs_bsp_version=}")

        # ------ sanity check, jetson-cboot is not used after BSP R34 ------ #
        if rootfs_bsp_version >= MAXIMUM_SUPPORTED_BSP_VERSION_EXCLUDE:
            _err_msg = (
                f"jetson-cboot only supports BSP version < {MAXIMUM_SUPPORTED_BSP_VERSION_EXCLUDE},"
                f" but get {rootfs_bsp_version=}. "
                f"Please use jetson-uefi control for device with BSP >= {MAXIMUM_SUPPORTED_BSP_VERSION_EXCLUDE}."
            )
            logger.error(_err_msg)
            raise JetsonCBootContrlError(_err_msg)

        # ------ load nvbootctrl config file ------ #
        if not (
            nvbootctrl_conf_fpath := Path(boot_cfg.NVBOOTCTRL_CONF_FPATH)
        ).is_file():
            _err_msg = "nv_boot_ctrl.conf is missing!"
            logger.error(_err_msg)
            raise JetsonCBootContrlError(_err_msg)
        self.nvbootctrl_conf = nvbootctrl_conf_fpath.read_text()
        logger.info(f"nvboot_ctrl_conf: \n{self.nvbootctrl_conf}")

        # ------ check if unified A/B is enabled ------ #
        # NOTE: mismatch rootfs BSP version and bootloader firmware BSP version
        #   is NOT supported and MUST not occur.
        unified_ab_enabled = False
        if rootfs_bsp_version >= BSPVersion(32, 6, 1):
            if unified_ab_enabled := NVBootctrlJetsonCBOOT.is_unified_enabled():
                logger.info(
                    "unified A/B is enabled, rootfs and bootloader will be switched together"
                )
        self.unified_ab_enabled = unified_ab_enabled

        # ------ check A/B slots ------ #
        try:
            self.current_bootloader_slot = current_bootloader_slot = (
                NVBootctrlJetsonCBOOT.get_current_slot()
            )
            self.standby_bootloader_slot = standby_bootloader_slot = (
                NVBootctrlJetsonCBOOT.get_standby_slot()
            )
            if not self.unified_ab_enabled:
                self.current_rootfs_slot = current_rootfs_slot = (
                    NVBootctrlJetsonCBOOT.get_current_slot(target="rootfs")
                )
                self.standby_rootfs_slot = standby_rootfs_slot = (
                    NVBootctrlJetsonCBOOT.get_standby_slot(target="rootfs")
                )
            else:
                self.current_rootfs_slot = current_rootfs_slot = current_bootloader_slot
                self.standby_rootfs_slot = standby_rootfs_slot = standby_bootloader_slot
        except NVBootctrlExecError as e:
            _err_msg = f"failed to detect slot info: {e!r}"
            logger.error(_err_msg)
            raise JetsonCBootContrlError(_err_msg) from e

        # check if rootfs slot and bootloader slot mismatches, this only happens
        #   when unified_ab is not enabled.
        if current_rootfs_slot != current_bootloader_slot:
            logger.warning(
                "bootloader and rootfs A/B slot mismatches: "
                f"{current_rootfs_slot=} != {current_bootloader_slot=}"
            )
            logger.warning("this might indicates a failed previous firmware update")

        # ------ detect rootfs_dev and parent_dev ------ #
        try:
            self.curent_rootfs_devpath = current_rootfs_devpath = (
                CMDHelperFuncs.get_current_rootfs_dev()
            )
            self.parent_devpath = parent_devpath = Path(
                CMDHelperFuncs.get_parent_dev(current_rootfs_devpath)
            )
        except Exception as e:
            _err_msg = f"failed to detect rootfs: {e!r}"
            logger.error(_err_msg)
            raise JetsonCBootContrlError(_err_msg) from e

        self._external_rootfs = detect_external_rootdev(parent_devpath)

        # rootfs partition
        try:
            self.standby_rootfs_devpath = get_partition_devpath(
                parent_devpath=parent_devpath,
                partition_id=SLOT_PAR_MAP[standby_rootfs_slot],
            )
            self.standby_rootfs_dev_partuuid = CMDHelperFuncs.get_attrs_by_dev(
                "PARTUUID", self.standby_rootfs_devpath
            ).strip()
            current_rootfs_dev_partuuid = CMDHelperFuncs.get_attrs_by_dev(
                "PARTUUID", current_rootfs_devpath
            ).strip()
        except Exception as e:
            _err_msg = f"failed to detect rootfs dev partuuid: {e!r}"
            logger.error(_err_msg)
            raise JetsonCBootContrlError(_err_msg) from e

        logger.info(
            "finish detecting rootfs devs: \n"
            f"active_slot({current_rootfs_slot}): {self.curent_rootfs_devpath=}, {current_rootfs_dev_partuuid=}\n"
            f"standby_slot({standby_rootfs_slot}): {self.standby_rootfs_devpath=}, {self.standby_rootfs_dev_partuuid=}"
        )

        # internal emmc partition
        self.standby_internal_emmc_devpath = get_partition_devpath(
            parent_devpath=f"/dev/{boot_cfg.INTERNAL_EMMC_DEVNAME}",
            partition_id=SLOT_PAR_MAP[standby_rootfs_slot],
        )
        logger.info(f"finished cboot control init: {current_rootfs_slot=}")
        logger.info(
            f"nvbootctrl dump-slots-info: \n{NVBootctrlJetsonCBOOT.dump_slots_info()}"
        )
        if not unified_ab_enabled:
            logger.info(
                f"nvbootctrl -t rootfs dump-slots-info: \n{NVBootctrlJetsonCBOOT.dump_slots_info(target='rootfs')}"
            )

    # API

    @property
    def external_rootfs_enabled(self) -> bool:
        """Indicate whether rootfs on external storage is enabled.

        NOTE: distiguish from boot from external storage, as R32.5 and below doesn't
            support native NVMe boot.
        """
        return self._external_rootfs

    def set_standby_rootfs_unbootable(self):
        NVBootctrlJetsonCBOOT.set_slot_as_unbootable(
            self.standby_rootfs_slot, target="rootfs"
        )

    def switch_boot_to_standby(self) -> None:
        # NOTE(20240412): we always try to align bootloader slot with rootfs.
        target_slot = self.standby_rootfs_slot

        logger.info(f"switch boot to standby slot({target_slot})")
        if not self.unified_ab_enabled:
            logger.info(
                f"unified AB slot is not enabled, also set active rootfs slot to ${target_slot}"
            )
            NVBootctrlJetsonCBOOT.set_active_boot_slot(target_slot, target="rootfs")

        # when unified_ab enabled, switching bootloader slot will also switch
        #   the rootfs slot.
        NVBootctrlJetsonCBOOT.set_active_boot_slot(target_slot)


class JetsonCBootControl(BootControllerProtocol):
    """BootControllerProtocol implementation for jetson-cboot."""

    def __init__(self) -> None:
        try:
            # startup boot controller
            self._cboot_control = cboot_control = _CBootControl()

            # mount point prepare
            self._mp_control = SlotMountHelper(
                standby_slot_dev=self._cboot_control.standby_rootfs_devpath,
                standby_slot_mount_point=cfg.MOUNT_POINT,
                active_slot_dev=self._cboot_control.curent_rootfs_devpath,
                active_slot_mount_point=cfg.ACTIVE_ROOT_MOUNT_POINT,
            )

            # init ota-status files
            current_ota_status_dir = Path(boot_cfg.OTA_STATUS_DIR)
            standby_ota_status_dir = Path(
                replace_root(
                    boot_cfg.OTA_STATUS_DIR,
                    "/",
                    cfg.MOUNT_POINT,
                )
            )
            self._ota_status_control = OTAStatusFilesControl(
                active_slot=str(self._cboot_control.current_rootfs_slot),
                standby_slot=str(self._cboot_control.standby_rootfs_slot),
                current_ota_status_dir=current_ota_status_dir,
                # NOTE: might not yet be populated before OTA update applied!
                standby_ota_status_dir=standby_ota_status_dir,
                finalize_switching_boot=self._finalize_switching_boot,
            )

            # load firmware BSP version
            current_fw_bsp_ver_fpath = (
                current_ota_status_dir / boot_cfg.FIRMWARE_BSP_VERSION_FNAME
            )
            self._firmware_bsp_ver_control = bsp_ver_ctrl = FirmwareBSPVersionControl(
                current_slot=cboot_control.current_bootloader_slot,
                # NOTE: see comments at L240-242
                current_slot_bsp_ver=cboot_control.rootfs_bsp_version,
                current_bsp_version_file=current_fw_bsp_ver_fpath,
            )
            # always update the bsp_version_file on startup to reflect
            #   the up-to-date current slot BSP version
            self._firmware_bsp_ver_control.write_to_file(current_fw_bsp_ver_fpath)
            logger.info(
                f"\ncurrent slot firmware BSP version: {bsp_ver_ctrl.current_slot_bsp_ver}\n"
                f"standby slot firmware BSP version: {bsp_ver_ctrl.standby_slot_bsp_ver}"
            )

            logger.info("jetson-cboot boot control start up finished")
        except Exception as e:
            _err_msg = f"failed to start jetson-cboot controller: {e!r}"
            raise ota_errors.BootControlStartupFailed(_err_msg, module=__name__) from e

    def _finalize_switching_boot(self) -> bool:
        """
        If firmware update failed(updated bootloader slot boot failed), clear the according slot's
            firmware_bsp_version information to force firmware update in next OTA.
        Also if unified A/B is NOT enabled and everything is alright, execute mark-boot-success <cur_slot>
            to mark the current booted rootfs boots successfully.
        """
        current_rootfs_slot = self._cboot_control.current_rootfs_slot

        update_result = NVUpdateEngine.verify_update()
        if (retcode := update_result.returncode) != 0:
            _err_msg = (
                f"The previous firmware update failed(verify return {retcode}): \n"
                f"stderr: {update_result.stderr.decode()}\n"
                f"stdout: {update_result.stdout.decode()}\n"
                "failing the OTA and clear firmware version due to new bootloader slot boot failed."
            )
            logger.error(_err_msg)
            return False

        # NOTE(20240417): rootfs slot is manually switched by set-active-boot-slot,
        #   so we need to manually set the slot as success after first reboot.
        if not self._cboot_control.unified_ab_enabled:
            logger.info(
                f"unified A/B is not enabled, set {current_rootfs_slot} boot succeeded."
            )
            NVBootctrlJetsonCBOOT.mark_boot_successful(
                current_rootfs_slot, target="rootfs"
            )

        logger.info(
            f"nv_update_engine verify succeeded: \n{update_result.stdout.decode()}"
        )
        return True

    def _firmware_update(self) -> bool | None:
        """Perform firmware update with nv_update_engine if needed.

        Returns:
            True if firmware update applied, False for failed firmware update,
                None for no firmware update occurs.
        """
        logger.info("jetson-cboot: entering nv firmware update ...")
        tnspec = get_nvbootctrl_conf_tnspec(self._cboot_control.nvbootctrl_conf)
        if not tnspec:
            logger.warning("tnspec is not defined, skip firmware update!")
            return

        firmware_package_meta = load_firmware_package(
            firmware_update_request_fpath=replace_root(
                boot_cfg.FIRMWARE_UPDATE_REQUEST_FPATH,
                "/",
                self._mp_control.standby_slot_mount_point,
            ),
            firmware_manifest_fpath=replace_root(
                boot_cfg.FIRMWARE_MANIFEST_FPATH,
                "/",
                self._mp_control.standby_slot_mount_point,
            ),
        )
        if firmware_package_meta is None:
            logger.info("skip firmware update ...")
            return
        firmware_update_request, firmware_manifest = firmware_package_meta

        # ------ preform firmware update ------ #
        fw_update_bsp_ver = BSPVersion.parse(
            firmware_manifest.firmware_spec.bsp_version
        )
        logger.info(f"firmware update package BSP version: {fw_update_bsp_ver}")

        firmware_updater = NVUpdateEngine(
            standby_slot_mp=self._mp_control.standby_slot_mount_point,
            tnspec=tnspec,
            firmware_update_request=firmware_update_request,
            firmware_manifest=firmware_manifest,
            unify_ab=bool(self._cboot_control.unified_ab_enabled),
        )
        return firmware_updater.firmware_update()

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
                # this operation not applicable when unified A/B is enabled.
                logger.info(
                    "unified AB slot is not enabled, set standby rootfs slot as unbootable"
                )
                self._cboot_control.set_standby_rootfs_unbootable()

            # prepare standby slot dev
            self._mp_control.prepare_standby_dev(erase_standby=erase_standby)
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
            update_standby_slot_extlinux_cfg(
                active_slot_extlinux_fpath=Path(boot_cfg.EXTLINUX_FILE),
                standby_slot_extlinux_fpath=self._mp_control.standby_slot_mount_point
                / Path(boot_cfg.EXTLINUX_FILE).relative_to("/"),
                standby_slot_partuuid=self._cboot_control.standby_rootfs_dev_partuuid,
            )

            # ------ firmware update ------ #
            firmware_update_result = self._firmware_update()
            if firmware_update_result is None:
                logger.info("no firmware update occurs")
            elif firmware_update_result is False:
                raise JetsonCBootContrlError("firmware update failed")
            else:
                logger.info("new firmware is written to the standby slot")

            # ------ preserve BSP version files to standby slot ------ #
            standby_fw_bsp_ver_fpath = (
                self._ota_status_control.standby_ota_status_dir
                / boot_cfg.FIRMWARE_BSP_VERSION_FNAME
            )
            self._firmware_bsp_ver_control.write_to_file(standby_fw_bsp_ver_fpath)

            # ------ preserve /boot/ota folder to standby rootfs ------ #
            preserve_ota_config_files_to_standby(
                active_slot_ota_dirpath=self._mp_control.active_slot_mount_point
                / "boot"
                / "ota",
                standby_slot_ota_dirpath=self._mp_control.standby_slot_mount_point
                / "boot"
                / "ota",
            )

            # ------ for external rootfs, preserve /boot folder to internal ------ #
            # NOTE: the copy must happen AFTER all the changes to active slot's /boot done.
            if self._cboot_control._external_rootfs:
                logger.info(
                    "rootfs on external storage enabled: "
                    "copy standby slot rootfs' /boot folder "
                    "to corresponding internal emmc dev ..."
                )
                copy_standby_slot_boot_to_internal_emmc(
                    internal_emmc_mp=Path(boot_cfg.SEPARATE_BOOT_MOUNT_POINT),
                    internal_emmc_devpath=Path(
                        self._cboot_control.standby_internal_emmc_devpath
                    ),
                    standby_slot_boot_dirpath=self._mp_control.standby_slot_mount_point
                    / "boot",
                )

            # ------ switch boot to standby ------ #
            self._cboot_control.switch_boot_to_standby()

            # ------ prepare to reboot ------ #
            self._mp_control.umount_all(ignore_error=True)
            logger.info(f"[post-update]: \n{NVBootctrlJetsonCBOOT.dump_slots_info()}")
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
            self._cboot_control.switch_boot_to_standby()
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

    def get_booted_ota_status(self) -> OTAStatus:
        return self._ota_status_control.booted_ota_status
