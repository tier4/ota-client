from pathlib import Path

from app.boot_control._grub import OtaPartitionFile
from app.boot_control.common import (
    CMDHelperFuncs,
    OTAStatusMixin,
    VersionControlMixin,
    BootControllerProtocol,
)
from app.configs import config as cfg
from app.ota_status import OTAStatusEnum
from app import log_util

assert cfg.BOOTLOADER == "grub"

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class GrubController(VersionControlMixin, OTAStatusMixin, BootControllerProtocol):
    def __init__(self) -> None:
        self._boot_control = OtaPartitionFile()

        # load paths
        self.standby_slot_path = Path(cfg.MOUNT_POINT)
        self.standby_slot_path.mkdir(exist_ok=True)
        self.standby_slot_dev = (
            Path("/dev") / self._boot_control.get_standby_root_device()
        )

        ## ota-status dir
        ### current slot: /boot/ota-partition.<rootfs_dev_active>
        # NOTE: BOOT_OTA_PARTITION_FILE is a directory, should we change it?
        self.current_ota_status_dir = (
            Path(cfg.BOOT_DIR)
            / f"{cfg.BOOT_OTA_PARTITION_FILE}.{self._boot_control.get_active_root_device()}"
        )
        self.current_ota_status_dir.mkdir(parents=True, exist_ok=True)

        ## standby slot: /boot/ota-partition.<rootfs_dev_standby>
        self.standby_ota_status_dir = (
            Path(cfg.BOOT_DIR)
            / f"{cfg.BOOT_OTA_PARTITION_FILE}.{self._boot_control.get_standby_root_device()}"
        )
        self.standby_ota_status_dir.mkdir(exist_ok=True)

        self.ota_status = self._init_boot_control()

    def _finalize_update(self) -> OTAStatusEnum:
        if self._boot_control.is_switching_boot_partition_from_active_to_standby():
            self._boot_control.update_grub_cfg()
            # switch should be called last.
            self._boot_control.switch_boot_partition_from_active_to_standby()
            return OTAStatusEnum.SUCCESS
        else:
            return OTAStatusEnum.FAILURE

    _finalize_rollback = _finalize_update

    def _init_boot_control(self) -> OTAStatusEnum:
        _ota_status = self._load_current_ota_status()

        if _ota_status == OTAStatusEnum.UPDATING:
            _ota_status = self._finalize_update()
        elif _ota_status == OTAStatusEnum.ROLLBACKING:
            _ota_status = self._finalize_rollback()

        # NOTE: only update the current ota_status at ota-client launching up!
        self._store_current_ota_status(_ota_status)
        logger.info(f"loaded ota_status: {_ota_status}")
        return _ota_status

    def _mount_standby(self, erase_standby: bool):
        # mount standby slot
        self._boot_control.cleanup_standby_boot_partition()
        if erase_standby:
            self._boot_control.mount_standby_root_partition_and_clean(
                self.standby_slot_path
            )
        else:
            # directly mount standby without cleaning up
            CMDHelperFuncs.mount(str(self.standby_slot_dev), self.standby_slot_path)

    def _mount_refroot(self, standby_as_ref: bool):
        CMDHelperFuncs.mount_refroot(
            standby_slot_dev=self._boot_control.get_standby_root_device(),
            active_slot_dev=self._boot_control.get_active_root_device(),
            standby_as_ref=standby_as_ref,
        )

    def _on_operation_failure(self, e: Exception):
        """Failure registering and cleanup at failure."""
        self._store_standby_ota_status(OTAStatusEnum.FAILURE)

        try:
            logger.warning("on failure try to unmounting standby slot...")
            CMDHelperFuncs.umount_dev(str(self.standby_slot_dev))
        finally:
            raise e

    ###### public methods ######
    # also includes methods from OTAStatusMixin, VersionControlMixin

    def get_standby_slot_path(self) -> Path:
        return self.standby_slot_path

    def get_standby_boot_dir(self) -> Path:
        """
        NOTE: in grub_controller, kernel and initrd images are stored under
        the ota_status_dir(ota_partition_dir)
        """
        return self.standby_ota_status_dir

    def pre_update(self, version: str, *, standby_as_ref: bool, erase_standby=False):
        try:
            self._store_standby_ota_status(OTAStatusEnum.UPDATING)
            self._store_standby_version(version)
            self._mount_standby(erase_standby)
            self._mount_refroot(standby_as_ref)
        except Exception as e:  # TODO: use BootControlError for any exceptions
            logger.error(f"failed on pre_update: {e!r}")
            self._on_operation_failure(e)

    def post_update(self):
        try:
            self._boot_control.update_fstab(self.standby_slot_path)
            self._boot_control.create_custom_cfg_and_reboot()
        except Exception as e:  # TODO: use BootControlError for any exceptions
            logger.error(f"failed on pre_update: {e!r}")
            self._on_operation_failure(e)

    def post_rollback(self):
        try:
            self._boot_control.create_custom_cfg_and_reboot(rollback=True)
        except Exception as e:
            logger.error(f"failed on pre_rollback: {e!r}")
            self._on_operation_failure(e)
