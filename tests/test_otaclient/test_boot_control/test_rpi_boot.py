import logging
import os
import shutil
import typing
from pathlib import Path
from string import Template

import pytest
import pytest_mock

from otaclient.app.boot_control import _rpi_boot
from otaclient.app.boot_control.configs import rpi_boot_cfg
from otaclient_api.v2 import types as api_types
from tests.conftest import TestConfiguration as cfg
from tests.utils import SlotMeta

logger = logging.getLogger(__name__)


class _RPIBootTestCfg:
    # slot config
    SLOT_A = "slot_a"
    SLOT_B = "slot_b"
    SLOT_A_DEV = "slot_a_dev"
    SLOT_B_DEV = "slot_b_dev"
    SEP_CHAR = "_"

    # dummy boot files content
    CONFIG_TXT_SLOT_A = "config_txt_slot_a"
    CONFIG_TXT_SLOT_B = "config_txt_slot_b"
    CMDLINE_TXT_SLOT_A = "cmdline_txt_slot_a"
    CMDLINE_TXT_SLOT_B = "cmdline_txt_slot_b"

    # module path
    rpi_boot__RPIBootControl_MODULE = f"{cfg.RPI_BOOT_MODULE_PATH}._RPIBootControl"
    rpi_boot_RPIBoot_CMDHelperFuncs_MODULE = (
        f"{cfg.RPI_BOOT_MODULE_PATH}.CMDHelperFuncs"
    )
    boot_control_common_CMDHelperFuncs_MODULE = (
        f"{cfg.BOOT_CONTROL_COMMON_MODULE_PATH}.CMDHelperFuncs"
    )

    # image version
    VERSION = "rpi_boot_test"


class RPIBootABPartitionFSM:
    def __init__(self) -> None:
        self._active_slot = _RPIBootTestCfg.SLOT_A
        self._standby_slot = _RPIBootTestCfg.SLOT_B
        self._active_slot_dev = _RPIBootTestCfg.SLOT_A_DEV
        self._standby_slot_dev = _RPIBootTestCfg.SLOT_B_DEV
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


class _RebootEXP(BaseException):
    """NOTE: use BaseException to escape normal Exception catch."""

    ...


class TestRPIBootControl:
    """
    Simulating otaclient starts from slot_a, and apply ota_update to slot_b
    """

    @pytest.fixture
    def rpi_boot_ab_slot(self, tmp_path: Path, ab_slots: SlotMeta):
        self.slot_a_mp = Path(ab_slots.slot_a)
        self.slot_b_mp = Path(ab_slots.slot_b)

        # setup ota_status dir for slot_a
        self.slot_a_ota_status_dir = self.slot_a_mp / Path(
            rpi_boot_cfg.OTA_STATUS_DIR
        ).relative_to("/")
        self.slot_a_ota_status_dir.mkdir(parents=True, exist_ok=True)
        slot_a_ota_status = self.slot_a_ota_status_dir / "status"
        slot_a_ota_status.write_text("SUCCESS")
        slot_a_version = self.slot_a_ota_status_dir / "version"
        slot_a_version.write_text(cfg.CURRENT_VERSION)
        slot_a_slot_in_use = self.slot_a_ota_status_dir / "slot_in_use"
        slot_a_slot_in_use.write_text(_RPIBootTestCfg.SLOT_A)
        # setup ota dir for slot_a
        slot_a_ota_dir = self.slot_a_mp / "boot" / "ota"
        slot_a_ota_dir.mkdir(parents=True, exist_ok=True)

        # setup /etc dir for slot_b
        (self.slot_b_mp / "etc").mkdir(parents=True, exist_ok=True)

        # setup ota_status dir for slot_b
        self.slot_b_ota_status_dir = self.slot_b_mp / Path(
            rpi_boot_cfg.OTA_STATUS_DIR
        ).relative_to("/")

        # setup shared system-boot
        self.system_boot = tmp_path / "system-boot"
        self.system_boot.mkdir(parents=True, exist_ok=True)
        # NOTE: primary config.txt is for slot_a at the beginning
        (self.system_boot / f"{_rpi_boot.CONFIG_TXT}").write_text(
            _RPIBootTestCfg.CONFIG_TXT_SLOT_A
        )
        # NOTE: rpi_boot controller now doesn't check the content of boot files, but only ensure the existence
        (
            self.system_boot
            / f"{_rpi_boot.CONFIG_TXT}{_RPIBootTestCfg.SEP_CHAR}{_RPIBootTestCfg.SLOT_A}"
        ).write_text(_RPIBootTestCfg.CONFIG_TXT_SLOT_A)
        (
            self.system_boot
            / f"{_rpi_boot.CONFIG_TXT}{_RPIBootTestCfg.SEP_CHAR}{_RPIBootTestCfg.SLOT_B}"
        ).write_text(_RPIBootTestCfg.CONFIG_TXT_SLOT_B)
        (
            self.system_boot
            / f"{_rpi_boot.CMDLINE_TXT}{_RPIBootTestCfg.SEP_CHAR}{_RPIBootTestCfg.SLOT_A}"
        ).write_text(_RPIBootTestCfg.CMDLINE_TXT_SLOT_A)
        (
            self.system_boot
            / f"{_rpi_boot.CMDLINE_TXT}{_RPIBootTestCfg.SEP_CHAR}{_RPIBootTestCfg.SLOT_B}"
        ).write_text(_RPIBootTestCfg.CMDLINE_TXT_SLOT_B)
        (
            self.system_boot
            / f"{_rpi_boot.VMLINUZ}{_RPIBootTestCfg.SEP_CHAR}{_RPIBootTestCfg.SLOT_A}"
        ).write_text("slot_a_vmlinux")
        (
            self.system_boot
            / f"{_rpi_boot.INITRD_IMG}{_RPIBootTestCfg.SEP_CHAR}{_RPIBootTestCfg.SLOT_A}"
        ).write_text("slot_a_initrdimg")
        self.vmlinuz_slot_b = (
            self.system_boot
            / f"{_rpi_boot.VMLINUZ}{_RPIBootTestCfg.SEP_CHAR}{_RPIBootTestCfg.SLOT_B}"
        )
        self.initrd_img_slot_b = (
            self.system_boot
            / f"{_rpi_boot.INITRD_IMG}{_RPIBootTestCfg.SEP_CHAR}{_RPIBootTestCfg.SLOT_B}"
        )

    @pytest.fixture(autouse=True)
    def mock_setup(self, mocker: pytest_mock.MockerFixture, rpi_boot_ab_slot):
        from otaclient.app.boot_control._common import CMDHelperFuncs
        from otaclient.app.boot_control._rpi_boot import _RPIBootControl

        # start the test FSM
        self._fsm = RPIBootABPartitionFSM()

        # mocking _RPIBootControl
        _RPIBootControl.standby_slot = mocker.PropertyMock(wraps=self._fsm.get_standby_slot)  # type: ignore
        _RPIBootControl.active_slot = mocker.PropertyMock(wraps=self._fsm.get_active_slot)  # type: ignore
        _RPIBootControl.active_slot_dev = mocker.PropertyMock(wraps=self._fsm.get_active_slot_dev)  # type: ignore
        _RPIBootControl.standby_slot_dev = mocker.PropertyMock(wraps=self._fsm.get_standby_slot_dev)  # type: ignore
        _RPIBootControl._init_slots_info = mocker.Mock()
        _RPIBootControl.reboot_tryboot = mocker.Mock(
            side_effect=self._fsm.reboot_tryboot
        )
        _RPIBootControl.update_firmware = mocker.Mock()

        # patch boot_control module
        self._mocked__rpiboot_control = _RPIBootControl
        mocker.patch(_RPIBootTestCfg.rpi_boot__RPIBootControl_MODULE, _RPIBootControl)

        # patch CMDHelperFuncs
        # NOTE: also remember to patch CMDHelperFuncs in common
        self._CMDHelper_mock = typing.cast(
            CMDHelperFuncs, mocker.MagicMock(spec=CMDHelperFuncs)
        )
        self._CMDHelper_mock.is_target_mounted = mocker.Mock(return_value=True)
        # NOTE: rpi_boot only call CMDHelperFuncs.reboot once in finalize_switch_boot method
        self._CMDHelper_mock.reboot = mocker.Mock(side_effect=_RebootEXP("reboot"))
        mocker.patch(
            _RPIBootTestCfg.rpi_boot_RPIBoot_CMDHelperFuncs_MODULE, self._CMDHelper_mock
        )
        mocker.patch(
            _RPIBootTestCfg.boot_control_common_CMDHelperFuncs_MODULE,
            self._CMDHelper_mock,
        )

    def test_rpi_boot_normal_update(self, mocker: pytest_mock.MockerFixture):
        from otaclient.app.boot_control._rpi_boot import RPIBootController

        # ------ patch rpi_boot_cfg for boot_controller_inst1.stage 1~3 ------#
        _rpi_boot_cfg_path = f"{cfg.BOOT_CONTROL_CONFIG_MODULE_PATH}.rpi_boot_cfg"
        mocker.patch(
            f"{_rpi_boot_cfg_path}.SYSTEM_BOOT_MOUNT_POINT", str(self.system_boot)
        )
        mocker.patch(f"{_rpi_boot_cfg_path}.ACTIVE_ROOTFS_PATH", str(self.slot_a_mp))
        mocker.patch(f"{_rpi_boot_cfg_path}.MOUNT_POINT", str(self.slot_b_mp))
        mocker.patch(
            f"{_rpi_boot_cfg_path}.ACTIVE_ROOT_MOUNT_POINT", str(self.slot_a_mp)
        )

        # ------ boot_controller_inst1.stage1: init ------ #
        rpi_boot_controller1 = RPIBootController()

        # ------ boot_controller_inst1.stage2: pre_update ------ #
        # --- execution --- #
        self.version = _RPIBootTestCfg.VERSION
        rpi_boot_controller1.pre_update(
            version=self.version,
            standby_as_ref=False,
            erase_standby=False,
        )
        # --- assertions --- #
        # 1. make sure the ota-status is updated properly
        # 2. make sure the mount points are prepared
        assert (
            self.slot_a_ota_status_dir / "status"
        ).read_text() == api_types.StatusOta.FAILURE.name
        assert (
            self.slot_b_ota_status_dir / "status"
        ).read_text() == api_types.StatusOta.UPDATING.name
        assert (
            (self.slot_a_ota_status_dir / "slot_in_use").read_text()
            == (self.slot_b_ota_status_dir / "slot_in_use").read_text()
            == _RPIBootTestCfg.SLOT_B
        )
        self._CMDHelper_mock.mount_rw.assert_called_once_with(
            target=self._fsm._standby_slot_dev, mount_point=self.slot_b_mp
        )
        self._CMDHelper_mock.mount_ro.assert_called_once_with(
            target=self._fsm._active_slot_dev, mount_point=self.slot_a_mp
        )

        # ------ mocked in_update ------ #
        # this should be done by create_standby module, so we do it manually here instead
        self.slot_b_boot_dir = self.slot_b_mp / "boot"
        self.slot_a_boot_dir = self.slot_a_mp / "boot"
        # NOTE: copy slot_a's kernel and initrd.img to slot_b,
        #       because we skip the create_standby step
        # NOTE 2: not copy the symlinks
        _vmlinuz = self.slot_a_boot_dir / "vmlinuz"
        shutil.copy(os.path.realpath(_vmlinuz), self.slot_b_boot_dir)
        _initrd_img = self.slot_a_boot_dir / "initrd.img"
        shutil.copy(os.path.realpath(_initrd_img), self.slot_b_boot_dir)

        # ------ boot_controller_inst1.stage3: post_update, reboot switch boot ------ #
        # --- execution --- #
        _post_updater = rpi_boot_controller1.post_update()
        next(_post_updater)
        next(_post_updater, None)  # actual reboot here
        # --- assertions: --- #
        # 1. make sure that retry boot is called
        # 2. make sure that fstab file is updated for slot_b
        # 3. make sure flash-kernel is called
        # 4. make sure tryboot.txt is presented and correct
        # 5. make sure config.txt is untouched
        self._mocked__rpiboot_control.reboot_tryboot.assert_called_once()
        self._mocked__rpiboot_control.update_firmware.assert_called_once()
        assert self._fsm.is_switched_boot
        assert (
            self.slot_b_mp / Path(rpi_boot_cfg.FSTAB_FPATH).relative_to("/")
        ).read_text() == Template(_rpi_boot._FSTAB_TEMPLATE_STR).substitute(
            rootfs_fslabel=_RPIBootTestCfg.SLOT_B
        )
        assert self.initrd_img_slot_b.is_file()
        assert self.vmlinuz_slot_b.is_file()
        assert (
            self.system_boot / "tryboot.txt"
        ).read_text() == _RPIBootTestCfg.CONFIG_TXT_SLOT_B
        assert (
            self.system_boot / "config.txt"
        ).read_text() == _RPIBootTestCfg.CONFIG_TXT_SLOT_A

        # ------ boot_controller_inst2: first reboot ------ #
        # patch rpi_boot_cfg for boot_controller_inst2
        _rpi_boot_cfg_path = f"{cfg.BOOT_CONTROL_CONFIG_MODULE_PATH}.rpi_boot_cfg"
        mocker.patch(f"{_rpi_boot_cfg_path}.ACTIVE_ROOTFS_PATH", str(self.slot_b_mp))
        mocker.patch(f"{_rpi_boot_cfg_path}.MOUNT_POINT", str(self.slot_a_mp))

        # ------ boot_controller_inst2.stage1: first reboot finalizing switch boot and update firmware ------ #
        logger.info("1st reboot: finalize switch boot and update firmware....")
        # --- execution --- #
        # NOTE: raise a _RebootEXP to simulate reboot and interrupt the otaclient
        with pytest.raises(_RebootEXP):
            RPIBootController()  # NOTE: init only
        # --- assertions: --- #
        # 1. assert that otaclient reboots the device
        # 2. assert firmware update is called
        # 3. assert reboot is called
        # 4. assert switch boot finalized
        # 5. assert slot_in_use is slot_b
        # 6. make sure the SWITCH_BOOT_FLAG_FILE file is created
        # 7. make sure ota_status is still UPDATING
        self._CMDHelper_mock.reboot.assert_called_once()
        assert (
            self.system_boot / "config.txt"
        ).read_text() == _RPIBootTestCfg.CONFIG_TXT_SLOT_B
        assert (
            self.slot_b_ota_status_dir / rpi_boot_cfg.SLOT_IN_USE_FNAME
        ).read_text() == "slot_b"
        assert (self.system_boot / rpi_boot_cfg.SWITCH_BOOT_FLAG_FILE).is_file()
        assert (
            self.slot_b_ota_status_dir / rpi_boot_cfg.OTA_STATUS_FNAME
        ).read_text() == api_types.StatusOta.UPDATING.name

        # ------ boot_controller_inst3.stage1: second reboot, apply updated firmware and finish up ota update ------ #
        logger.info("2nd reboot: finish up ota update....")
        # --- execution --- #
        rpi_boot_controller4_2 = RPIBootController()
        # --- assertions: --- #
        # 1. make sure ota_status is SUCCESS
        # 2. make sure the flag file is cleared
        # 3. make sure the config.txt is still for slot_b
        assert (
            rpi_boot_controller4_2.get_booted_ota_status()
            == api_types.StatusOta.SUCCESS
        )
        assert (
            self.slot_b_ota_status_dir / rpi_boot_cfg.OTA_STATUS_FNAME
        ).read_text() == api_types.StatusOta.SUCCESS.name
        assert not (self.system_boot / rpi_boot_cfg.SWITCH_BOOT_FLAG_FILE).is_file()
        assert (
            rpi_boot_controller4_2._ota_status_control._load_current_slot_in_use()
            == "slot_b"
        )
