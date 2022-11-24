import os
import pytest
import pytest_mock
import shutil
import typing
from pathlib import Path

from tests.utils import SlotMeta
from tests.conftest import TestConfiguration as cfg
from otaclient.app.boot_control.configs import rpi_boot_cfg

import logging

logger = logging.getLogger(__name__)


class RPIBootABPartitionFSM:
    SLOT_A = rpi_boot_cfg.SLOT_A_FSLABEL
    SLOT_B = rpi_boot_cfg.SLOT_B_FSLABEL
    SEP_CHAR = "_"

    def __init__(self) -> None:
        self._active_slot = self.SLOT_A
        self._standby_slot = self.SLOT_B
        self._active_slot_dev = "slot_a_dev"
        self._standby_slot_dev = "slot_b_dev"
        self.is_switched_boot = False

    def reboot_tryboot(self):
        logger.info(f"tryboot to {self._standby_slot=}")
        self.is_switched_boot = True
        self._active_slot, self._standby_slot = self._standby_slot, self._active_slot
        self._active_slot_dev, self._standby_slot_dev = (
            self._standby_slot_dev,
            self._active_slot_dev,
        )

    def get_active_slot(self) -> str:
        return self._active_slot

    def get_standby_slot(self) -> str:
        return self._standby_slot

    def get_standby_slot_dev(self) -> str:
        return self._standby_slot_dev

    def get_active_slot_dev(self) -> str:
        return self._active_slot_dev


class TestRPIBootControl:
    """
    Simulating otaclient starts from slot_a, and apply ota_update to slot_b
    """

    SLOT_A = rpi_boot_cfg.SLOT_A_FSLABEL
    SLOT_B = rpi_boot_cfg.SLOT_B_FSLABEL
    SEP_CHAR = "_"

    @pytest.fixture
    def rpi_boot_ab_slot(self, tmp_path: Path, ab_slots: SlotMeta):
        self.slot_a_mp = Path(ab_slots.slot_a)
        self.slot_b_mp = Path(ab_slots.slot_b)

        # setup ota_status dir for slot_a
        self.slot_a_ota_status_dir = self.slot_a_mp / Path(
            rpi_boot_cfg.OTA_STATUS_DIR
        ).relative_to("/")
        self.slot_a_ota_status_dir.mkdir(parents=True)
        slot_a_ota_status = self.slot_a_ota_status_dir / "status"
        slot_a_ota_status.write_text("SUCCESS")
        slot_a_version = self.slot_a_ota_status_dir / "version"
        slot_a_version.write_text(cfg.CURRENT_VERSION)
        slot_a_slot_in_use = self.slot_a_ota_status_dir / "slot_in_use"
        slot_a_slot_in_use.write_text(rpi_boot_cfg.SLOT_A_FSLABEL)

        # setup ota_status dir for slot_b
        self.slot_b_ota_status_dir = self.slot_b_mp / Path(
            rpi_boot_cfg.OTA_STATUS_DIR
        ).relative_to("/")

        # setup shared system-boot
        self.system_boot = tmp_path / "system-boot"
        self.system_boot.mkdir(parents=True, exist_ok=True)
        # NOTE: rpi_boot controller now doesn't check the content of boot files, but only ensure the existence
        (
            self.system_boot / f"{rpi_boot_cfg.CONFIG_TXT}{self.SEP_CHAR}{self.SLOT_A}"
        ).write_text("slot_a_conf_txt")
        (
            self.system_boot / f"{rpi_boot_cfg.CONFIG_TXT}{self.SEP_CHAR}{self.SLOT_B}"
        ).write_text("slot_b_conf_txt")
        (
            self.system_boot / f"{rpi_boot_cfg.CMDLINE_TXT}{self.SEP_CHAR}{self.SLOT_A}"
        ).write_text("slot_a_cmdline_txt")
        (
            self.system_boot / f"{rpi_boot_cfg.CMDLINE_TXT}{self.SEP_CHAR}{self.SLOT_B}"
        ).write_text("slot_b_cmdline_txt")
        (
            self.system_boot / f"{rpi_boot_cfg.VMLINUZ}{self.SEP_CHAR}{self.SLOT_A}"
        ).write_text("slot_a_vmlinux")
        (
            self.system_boot / f"{rpi_boot_cfg.INITRD_IMG}{self.SEP_CHAR}{self.SLOT_A}"
        ).write_text("slot_a_initrdimg")

    @pytest.fixture(autouse=True)
    def mock_setup(self, mocker: pytest_mock.MockerFixture, rpi_boot_ab_slot):
        from otaclient.app.boot_control._rpi_boot import _RPIBootControl
        from otaclient.app.boot_control._common import CMDHelperFuncs

        ### start the test FSM ###
        self._fsm = RPIBootABPartitionFSM()

        ### mocking _RPIBootControl ###
        _RPIBootControl.standby_slot = mocker.PropertyMock(wraps=self._fsm.get_standby_slot)  # type: ignore
        _RPIBootControl.active_slot = mocker.PropertyMock(wraps=self._fsm.get_active_slot)  # type: ignore
        _RPIBootControl.active_slot_dev = mocker.PropertyMock(wraps=self._fsm.get_active_slot_dev)  # type: ignore
        _RPIBootControl.standby_slot_dev = mocker.PropertyMock(wraps=self._fsm.get_standby_slot_dev)  # type: ignore
        _RPIBootControl._init_slots_info = mocker.Mock()
        _RPIBootControl.reboot_tryboot = mocker.Mock(
            side_effect=self._fsm.reboot_tryboot
        )

        ### patch boot_control module ###
        mocker.patch(f"{cfg.RPI_BOOT_MODULE_PATH}._RPIBootControl", _RPIBootControl)

        # patch CMDHelperFuncs
        # NOTE: also remember to patch CMDHelperFuncs in common
        _CMDHelper_mock = typing.cast(
            CMDHelperFuncs, mocker.MagicMock(spec=CMDHelperFuncs)
        )
        _CMDHelper_mock.is_target_mounted = mocker.Mock(return_value=True)
        mocker.patch(f"{cfg.RPI_BOOT_MODULE_PATH}.CMDHelperFuncs", _CMDHelper_mock)
        mocker.patch(
            f"{cfg.BOOT_CONTROL_COMMON_MODULE_PATH}.CMDHelperFuncs", _CMDHelper_mock
        )

    def test_rpi_boot_normal_update(self, mocker: pytest_mock.MockerFixture):
        from otaclient.app.boot_control._rpi_boot import RPIBootController

        ### patch rpi_boot_cfg for stage 1~3 ###
        _rpi_boot_cfg_path = f"{cfg.BOOT_CONTROL_CONFIG_MODULE_PATH}.rpi_boot_cfg"
        mocker.patch(
            f"{_rpi_boot_cfg_path}.SYSTEM_BOOT_MOUNT_POINT", str(self.system_boot)
        )
        mocker.patch(f"{_rpi_boot_cfg_path}.ACTIVE_ROOTFS_PATH", str(self.slot_a_mp))
        mocker.patch(f"{_rpi_boot_cfg_path}.MOUNT_POINT", str(self.slot_b_mp))

        ### stage 1, init boot controller ###
        rpi_boot_controller = RPIBootController()

        ### stage 2, pre_update ###
        self.version = "rpi_boot test"
        rpi_boot_controller.pre_update(
            version=self.version,
            standby_as_ref=False,
            erase_standby=False,
        )
        # prepare kernel and initrd.img in slot_b's boot folder
        # this should be done by create_standby module, so we do it manually here instead
        self.slot_b_boot_dir = self.slot_b_mp / "boot"
        self.slot_a_boot_dir = self.slot_a_mp / "boot"
        # NOTE: copy slot_a's kernel and initrd.img to slot_b
        _vmlinuz = self.slot_a_boot_dir / "vmlinuz"
        shutil.copy(
            _vmlinuz,
            self.slot_b_boot_dir,
            follow_symlinks=False,
        )
        shutil.copy(os.path.realpath(_vmlinuz), self.slot_b_boot_dir)

        _initrd_img = self.slot_a_boot_dir / "initrd.img"
        shutil.copy(
            _initrd_img,
            self.slot_b_boot_dir,
            follow_symlinks=False,
        )
        shutil.copy(os.path.realpath(_initrd_img), self.slot_b_boot_dir)

        ### stage 3, post_update, reboot switch boot ###
        rpi_boot_controller.post_update()

        ### patch rpi_boot_cfg for stage 4 ###
        _rpi_boot_cfg_path = f"{cfg.BOOT_CONTROL_CONFIG_MODULE_PATH}.rpi_boot_cfg"
        mocker.patch(f"{_rpi_boot_cfg_path}.ACTIVE_ROOTFS_PATH", str(self.slot_b_mp))
        mocker.patch(f"{_rpi_boot_cfg_path}.MOUNT_POINT", str(self.slot_a_mp))

        ### stage 4, first reboot finalizing switch boot ###
        logger.info("first boot after tryboot...")
        rpi_boot_controller = RPIBootController()
