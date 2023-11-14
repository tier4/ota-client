import os
import pytest
import pytest_mock
import shutil
import typing
from pathlib import Path
from string import Template

from tests.utils import SlotMeta
from tests.conftest import TestConfiguration as test_cfg
from otaclient._utils.path import replace_root
from otaclient.app.configs import Config as otaclient_Config
from otaclient.app.boot_control._rpi_boot import _FSTAB_TEMPLATE_STR
from otaclient.app.boot_control.configs import RPIBootControlConfig
from otaclient.app.proto import wrapper

import logging

logger = logging.getLogger(__name__)


class _RPIBootTestCfg:
    # slot config
    SLOT_A = RPIBootControlConfig.SLOT_A_FSLABEL
    SLOT_B = RPIBootControlConfig.SLOT_B_FSLABEL
    SLOT_A_DEV = "slot_a_dev"
    SLOT_B_DEV = "slot_b_dev"
    SEP_CHAR = "_"

    # dummy boot files content
    CONFIG_TXT_SLOT_A = "config_txt_slot_a"
    CONFIG_TXT_SLOT_B = "config_txt_slot_b"
    CMDLINE_TXT_SLOT_A = "cmdline_txt_slot_a"
    CMDLINE_TXT_SLOT_B = "cmdline_txt_slot_b"

    # module path
    rpiboot_control_module_path = f"{test_cfg.RPI_BOOT_MODULE_PATH}._RPIBootControl"
    rpi_boot_RPIBoot_CMDHelperFuncs_MODULE = (
        f"{test_cfg.RPI_BOOT_MODULE_PATH}.CMDHelperFuncs"
    )
    boot_control_common_CMDHelperFuncs_MODULE = (
        f"{test_cfg.BOOT_CONTROL_COMMON_MODULE_PATH}.CMDHelperFuncs"
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
    def rpi_boot_ab_slot(self, ab_slots: SlotMeta):
        self.slot_a_pa = Path(ab_slots.slot_a)
        self.slot_b_pa = Path(ab_slots.slot_b)

        #
        # ------ init otaclient configs ------ #
        #
        # create otaclient config with different active rootfs
        self.mocked_otaclient_cfg_slot_a = otaclient_Config(
            ACTIVE_ROOTFS=str(self.slot_a_pa)
        )
        self.mocked_otaclient_cfg_slot_b = otaclient_Config(
            ACTIVE_ROOTFS=str(self.slot_b_pa)
        )

        #
        # ------ setup shared system-boot partition ------ #
        #
        # NOTE: rpi_boot uses shared system_boot partition, here we only use slot_a_boot_dev
        #       to simlulate such condition
        self.system_boot_mp = Path(ab_slots.slot_a_boot_dev)
        self.system_boot_mp.mkdir(parents=True, exist_ok=True)

        #
        # ------ setup system-boot mount point ------ #
        #
        # simulate rpi device mounts system-boot partition to /boot/firmware mountpoint
        slot_a_system_boot_mp = self.slot_a_pa / "boot" / "firmware"
        slot_a_system_boot_mp.symlink_to(self.system_boot_mp, target_is_directory=True)

        (self.slot_b_pa / "boot").mkdir(parents=True, exist_ok=True)
        slot_b_system_boot_mp = self.slot_b_pa / "boot" / "firmware"
        slot_b_system_boot_mp.symlink_to(self.system_boot_mp, target_is_directory=True)

        # setup etc folder for slot_b
        (self.slot_b_pa / "etc").mkdir(parents=True, exist_ok=True)

    @pytest.fixture(autouse=True)
    def mock_setup(self, mocker: pytest_mock.MockerFixture, rpi_boot_ab_slot):
        from otaclient.app.boot_control._rpi_boot import _RPIBootControl
        from otaclient.app.boot_control._common import CMDHelperFuncs

        #
        # ------ start the test FSM ------ #
        #
        self._fsm = RPIBootABPartitionFSM()

        #
        # ------ rpi_boot configs patch applying ------ #
        #
        mocker.patch(
            f"{test_cfg.BOOT_CONTROL_CONFIG_MODULE_PATH}.cfg",
            self.mocked_otaclient_cfg_slot_a,
        )
        mocker.patch(
            f"{test_cfg.RPI_BOOT_MODULE_PATH}.cfg", self.mocked_otaclient_cfg_slot_a
        )
        # NOTE: remember to also patch otaclient cfg in boot.common module
        mocker.patch(
            f"{test_cfg.BOOT_CONTROL_COMMON_MODULE_PATH}.cfg",
            self.mocked_otaclient_cfg_slot_a,
        )
        # after the mocked cfg is applied, we can init rpi_boot cfg instance
        self.mocked_boot_cfg_slot_a = RPIBootControlConfig()

        #
        # ------ prepare mocked _RPIBootControl ------ #
        #
        _mocked__rpi_boot_ctrl = typing.cast(
            "type[_RPIBootControl]",
            type("_mocked_RPIBootControl", (_RPIBootControl,), {}),
        )

        # bind slots related methods to test FSM
        _mocked__rpi_boot_ctrl.standby_slot = mocker.PropertyMock(  # type: ignore
            wraps=self._fsm.get_standby_slot
        )
        _mocked__rpi_boot_ctrl.active_slot = mocker.PropertyMock(  # type: ignore
            wraps=self._fsm.get_active_slot
        )
        _mocked__rpi_boot_ctrl.active_slot_dev = mocker.PropertyMock(  # type: ignore
            wraps=self._fsm.get_active_slot_dev
        )
        _mocked__rpi_boot_ctrl.standby_slot_dev = mocker.PropertyMock(  # type: ignore
            wraps=self._fsm.get_standby_slot_dev
        )
        _mocked__rpi_boot_ctrl.reboot_tryboot = mocker.Mock(
            side_effect=self._fsm.reboot_tryboot
        )
        _mocked__rpi_boot_ctrl.system_boot_path = mocker.PropertyMock(  # type: ignore
            return_value=self.system_boot_mp
        )

        # hide away this method as slots detection is handled by test FSM
        _mocked__rpi_boot_ctrl._init_slots_info = mocker.Mock()

        # mock firmware update method, only check if it is called
        _mocked__rpi_boot_ctrl._update_firmware = mocker.Mock()
        self.mocked__rpi_boot_ctrl_type = _mocked__rpi_boot_ctrl

        #
        # ------ patch CMDHelperFuncs ------ #
        #
        self._mocked_cmdhelper = typing.cast(
            CMDHelperFuncs, mocker.MagicMock(spec=CMDHelperFuncs)
        )
        # NOTE: especially patch this method, as _RPIBootControl's __init__ uses this method
        #       to check if system-boot folder is a mount point.
        self._mocked_cmdhelper.is_target_mounted = mocker.Mock(return_value=True)

        # NOTE: rpi_boot only call CMDHelperFuncs.reboot once in finalize_switch_boot method
        self._mocked_cmdhelper.reboot = mocker.Mock(side_effect=_RebootEXP("reboot"))
        mocker.patch(
            _RPIBootTestCfg.rpi_boot_RPIBoot_CMDHelperFuncs_MODULE,
            self._mocked_cmdhelper,
        )
        # NOTE: also remember to patch CMDHelperFuncs in boot_control.common module
        mocker.patch(
            _RPIBootTestCfg.boot_control_common_CMDHelperFuncs_MODULE,
            self._mocked_cmdhelper,
        )

    @pytest.fixture(autouse=True)
    def setup_test(self, mocker: pytest_mock.MockerFixture, mock_setup):
        _otaclient_cfg = self.mocked_otaclient_cfg_slot_a
        _rpi_boot_cfg = self.mocked_boot_cfg_slot_a

        #
        # ------ setup mount space ------ #
        #
        # NOTE: as we mock CMDHelpers, mount is not executed, so we prepare the mount points
        #   by ourselves.(In the future we can use FSM to do it.)
        Path(_otaclient_cfg.OTACLIENT_MOUNT_SPACE_DPATH).mkdir(
            parents=True, exist_ok=True
        )
        Path(_otaclient_cfg.ACTIVE_SLOT_MP).symlink_to(self.slot_a_pa)
        Path(_otaclient_cfg.STANDBY_SLOT_MP).symlink_to(self.slot_b_pa)

        #
        # ------ setup OTA status files ------ #
        #

        # setup slot a
        self.slot_a_ota_status_dir = Path(_rpi_boot_cfg.ACTIVE_BOOT_OTA_STATUS_DPATH)
        self.slot_a_ota_status_dir.mkdir(parents=True, exist_ok=True)

        slot_a_ota_dir = self.slot_a_pa / "boot" / "ota"
        slot_a_ota_dir.mkdir(parents=True, exist_ok=True)

        slot_a_ota_status = self.slot_a_ota_status_dir / _otaclient_cfg.OTA_STATUS_FNAME
        slot_a_ota_status.write_text(wrapper.StatusOta.SUCCESS.name)

        slot_a_version = self.slot_a_ota_status_dir / _otaclient_cfg.OTA_VERSION_FNAME
        slot_a_version.write_text(test_cfg.CURRENT_VERSION)
        slot_a_slot_in_use = (
            self.slot_a_ota_status_dir / _otaclient_cfg.SLOT_IN_USE_FNAME
        )
        slot_a_slot_in_use.write_text(_rpi_boot_cfg.SLOT_A_FSLABEL)

        # setup slot_b
        self.slot_b_ota_status_dir = Path(_rpi_boot_cfg.STANDBY_BOOT_OTA_STATUS_DPATH)
        self.slot_b_ota_status_dir.mkdir(parents=True, exist_ok=True)

        #
        # ------ setup shared system-boot partition ------ #
        #
        # NOTE: primary config.txt is for slot_a at the beginning
        (self.system_boot_mp / f"{_rpi_boot_cfg.CONFIG_TXT_FNAME}").write_text(
            _RPIBootTestCfg.CONFIG_TXT_SLOT_A
        )
        # NOTE: rpi_boot controller currently doesn't check the content of boot files, but only ensure the existence
        (
            self.system_boot_mp
            / f"{_rpi_boot_cfg.CONFIG_TXT_FNAME}{_RPIBootTestCfg.SEP_CHAR}{_RPIBootTestCfg.SLOT_A}"
        ).write_text(_RPIBootTestCfg.CONFIG_TXT_SLOT_A)
        (
            self.system_boot_mp
            / f"{_rpi_boot_cfg.CONFIG_TXT_FNAME}{_RPIBootTestCfg.SEP_CHAR}{_RPIBootTestCfg.SLOT_B}"
        ).write_text(_RPIBootTestCfg.CONFIG_TXT_SLOT_B)
        (
            self.system_boot_mp
            / f"{_rpi_boot_cfg.CMDLINE_TXT_FNAME}{_RPIBootTestCfg.SEP_CHAR}{_RPIBootTestCfg.SLOT_A}"
        ).write_text(_RPIBootTestCfg.CMDLINE_TXT_SLOT_A)
        (
            self.system_boot_mp
            / f"{_rpi_boot_cfg.CMDLINE_TXT_FNAME}{_RPIBootTestCfg.SEP_CHAR}{_RPIBootTestCfg.SLOT_B}"
        ).write_text(_RPIBootTestCfg.CMDLINE_TXT_SLOT_B)
        (
            self.system_boot_mp
            / f"{_rpi_boot_cfg.VMLINUZ_FNAME}{_RPIBootTestCfg.SEP_CHAR}{_RPIBootTestCfg.SLOT_A}"
        ).write_text("slot_a_vmlinux")
        (
            self.system_boot_mp
            / f"{_rpi_boot_cfg.INITRD_IMG_FNAME}{_RPIBootTestCfg.SEP_CHAR}{_RPIBootTestCfg.SLOT_A}"
        ).write_text("slot_a_initrdimg")
        self.vmlinuz_slot_b = (
            self.system_boot_mp
            / f"{_rpi_boot_cfg.VMLINUZ_FNAME}{_RPIBootTestCfg.SEP_CHAR}{_RPIBootTestCfg.SLOT_B}"
        )
        self.initrd_img_slot_b = (
            self.system_boot_mp
            / f"{_rpi_boot_cfg.INITRD_IMG_FNAME}{_RPIBootTestCfg.SEP_CHAR}{_RPIBootTestCfg.SLOT_B}"
        )

        #
        # ------ patch rpi_boot module for slot_a ------ #
        #
        mocker.patch(
            _RPIBootTestCfg.rpiboot_control_module_path, self.mocked__rpi_boot_ctrl_type
        )
        mocker.patch(f"{test_cfg.RPI_BOOT_MODULE_PATH}.boot_cfg", _rpi_boot_cfg)

    def test_rpi_boot_normal_update(self, mocker: pytest_mock.MockerFixture):
        from otaclient.app.boot_control._rpi_boot import RPIBootController

        _otaclient_cfg = self.mocked_otaclient_cfg_slot_a
        _rpi_boot_cfg = self.mocked_boot_cfg_slot_a

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
            self.slot_a_ota_status_dir / _otaclient_cfg.OTA_STATUS_FNAME
        ).read_text() == wrapper.StatusOta.FAILURE.name
        assert (
            self.slot_b_ota_status_dir / _otaclient_cfg.OTA_STATUS_FNAME
        ).read_text() == wrapper.StatusOta.UPDATING.name
        assert (
            (self.slot_a_ota_status_dir / _otaclient_cfg.SLOT_IN_USE_FNAME).read_text()
            == (
                self.slot_b_ota_status_dir / _otaclient_cfg.SLOT_IN_USE_FNAME
            ).read_text()
            == _RPIBootTestCfg.SLOT_B
        )

        self._mocked_cmdhelper.mount_rw.assert_called_once_with(
            target=self._fsm._standby_slot_dev,
            mount_point=Path(_otaclient_cfg.STANDBY_SLOT_MP),
        )
        self._mocked_cmdhelper.mount_ro.assert_called_once_with(
            target=self._fsm._active_slot_dev,
            mount_point=Path(_otaclient_cfg.ACTIVE_SLOT_MP),
        )

        # ------ mocked in_update ------ #
        # this should be done by create_standby module, so we do it manually here instead
        self.slot_b_boot_dir = self.slot_b_pa / "boot"
        self.slot_a_boot_dir = self.slot_a_pa / "boot"

        # NOTE: copy slot_a's kernel and initrd.img to slot_b,
        #       because we skip the create_standby step
        # NOTE 2: not copy the symlinks
        _vmlinuz = self.slot_a_boot_dir / _rpi_boot_cfg.VMLINUZ_FNAME
        shutil.copy(os.path.realpath(_vmlinuz), self.slot_b_boot_dir)
        _initrd_img = self.slot_a_boot_dir / _rpi_boot_cfg.INITRD_IMG_FNAME
        shutil.copy(os.path.realpath(_initrd_img), self.slot_b_boot_dir)

        # ------ boot_controller_inst1.stage3: post_update, reboot switch boot ------ #
        # --- execution --- #
        _post_updater = rpi_boot_controller1.post_update()
        next(_post_updater)
        next(_post_updater, None)  # actual reboot here
        # --- assertions: --- #
        # 1. make sure that retry boot is called
        # 2. make sure that fstab file is updated for slot_b
        # 3. assert kernel and initrd.img are copied to system-boot
        # 4. make sure tryboot.txt is presented and correct
        # 5. make sure config.txt is untouched
        self.mocked__rpi_boot_ctrl_type.reboot_tryboot.assert_called_once()
        assert self._fsm.is_switched_boot

        assert Path(
            replace_root(
                _otaclient_cfg.FSTAB_FPATH,
                _otaclient_cfg.ACTIVE_ROOTFS,
                self.slot_b_pa,
            )
        ).read_text() == Template(_FSTAB_TEMPLATE_STR).substitute(
            rootfs_fslabel=_RPIBootTestCfg.SLOT_B
        )

        assert self.initrd_img_slot_b.is_file()
        assert self.vmlinuz_slot_b.is_file()

        assert (
            self.system_boot_mp / _rpi_boot_cfg.TRYBOOT_TXT_FNAME
        ).read_text() == _RPIBootTestCfg.CONFIG_TXT_SLOT_B
        assert (
            self.system_boot_mp / _rpi_boot_cfg.CONFIG_TXT_FNAME
        ).read_text() == _RPIBootTestCfg.CONFIG_TXT_SLOT_A

        #
        # ------ boot_controller_inst2: first reboot ------ #
        #
        # Now active rootfs is slot_b.

        # patch rpi_boot_cfg for boot_controller_inst2
        _otaclient_cfg = self.mocked_otaclient_cfg_slot_b
        mocker.patch(f"{test_cfg.BOOT_CONTROL_CONFIG_MODULE_PATH}.cfg", _otaclient_cfg)
        mocker.patch(f"{test_cfg.RPI_BOOT_MODULE_PATH}.cfg", _otaclient_cfg)
        mocker.patch(f"{test_cfg.BOOT_CONTROL_COMMON_MODULE_PATH}.cfg", _otaclient_cfg)

        # after the mocked cfg is applied, we can init rpi_boot cfg instance
        _rpi_boot_cfg = RPIBootControlConfig()
        mocker.patch(f"{test_cfg.RPI_BOOT_MODULE_PATH}.boot_cfg", _rpi_boot_cfg)

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
        self.mocked__rpi_boot_ctrl_type._update_firmware.assert_called_once()
        self._mocked_cmdhelper.reboot.assert_called_once()
        assert (
            self.system_boot_mp / _rpi_boot_cfg.CONFIG_TXT_FNAME
        ).read_text() == _RPIBootTestCfg.CONFIG_TXT_SLOT_B
        assert (
            self.slot_b_ota_status_dir / _otaclient_cfg.SLOT_IN_USE_FNAME
        ).read_text() == "slot_b"
        assert (self.system_boot_mp / _rpi_boot_cfg.SWITCH_BOOT_FLAG_FNAME).is_file()
        assert (
            self.slot_b_ota_status_dir / _otaclient_cfg.OTA_STATUS_FNAME
        ).read_text() == wrapper.StatusOta.UPDATING.name

        # ------ boot_controller_inst3.stage1: second reboot, apply updated firmware and finish up ota update ------ #
        logger.info("2nd reboot: finish up ota update....")
        # --- execution --- #
        rpi_boot_controller4_2 = RPIBootController()
        # --- assertions: --- #
        # 1. make sure ota_status is SUCCESS
        # 2. make sure the flag file is cleared
        # 3. make sure the config.txt is still for slot_b
        assert (
            rpi_boot_controller4_2.get_booted_ota_status() == wrapper.StatusOta.SUCCESS
        )
        assert (
            self.slot_b_ota_status_dir / _otaclient_cfg.OTA_STATUS_FNAME
        ).read_text() == wrapper.StatusOta.SUCCESS.name
        assert not (
            self.system_boot_mp / _rpi_boot_cfg.SWITCH_BOOT_FLAG_FNAME
        ).is_file()
        assert (
            rpi_boot_controller4_2._ota_status_control._load_current_slot_in_use()
            == "slot_b"
        )
