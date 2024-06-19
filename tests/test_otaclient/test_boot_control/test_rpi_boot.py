import logging
import os
import shutil
import typing
from pathlib import Path
from string import Template

import pytest
import pytest_mock

from otaclient.app.boot_control import _rpi_boot
from otaclient.app.boot_control._common import CMDHelperFuncs, SlotMountHelper
from otaclient.app.boot_control.configs import rpi_boot_cfg
from otaclient_api.v2 import types as api_types
from tests.conftest import TestConfiguration as cfg
from tests.utils import SlotMeta

logger = logging.getLogger(__name__)

# slot config
SLOT_A = "slot_a"
SLOT_B = "slot_b"
SLOT_A_DEV = "/dev/sda2"
SLOT_B_DEV = "/dev/sda3"
SEP_CHAR = "_"

# dummy boot files content
CONFIG_TXT_SLOT_A = "config_txt_slot_a"
CONFIG_TXT_SLOT_B = "config_txt_slot_b"
CMDLINE_TXT_SLOT_A = "cmdline_txt_slot_a"
CMDLINE_TXT_SLOT_B = "cmdline_txt_slot_b"

# module path
RPI_BOOT_MODULE_PATH = "otaclient.app.boot_control._rpi_boot"
rpi_boot__RPIBootControl_MODULE = f"{RPI_BOOT_MODULE_PATH}._RPIBootControl"
rpi_boot_RPIBoot_CMDHelperFuncs_MODULE = f"{RPI_BOOT_MODULE_PATH}.CMDHelperFuncs"
boot_control_common_CMDHelperFuncs_MODULE = (
    f"{cfg.BOOT_CONTROL_COMMON_MODULE_PATH}.CMDHelperFuncs"
)

# image version
VERSION = "rpi_boot_test"


class RPIBootABPartitionFSM:
    def __init__(self) -> None:
        self.active_slot = SLOT_A
        self.standby_slot = SLOT_B
        self.active_slot_dev = SLOT_A_DEV
        self.standby_slot_dev = SLOT_B_DEV
        self.parent_dev = "/dev/sda"
        self.is_switched_boot = False

    def reboot_tryboot(self):
        logger.info(f"tryboot to {self.standby_slot=}")
        self.is_switched_boot = True
        self.active_slot, self.standby_slot = self.standby_slot, self.active_slot
        self.active_slot_dev, self.standby_slot_dev = (
            self.standby_slot_dev,
            self.active_slot_dev,
        )

    def get_current_rootfs_dev(self):
        return self.active_slot_dev

    def get_parent_dev(self, *args, **kwags):
        return self.parent_dev

    def get_device_tree(self, parent_dev):
        assert parent_dev == self.parent_dev
        # NOTE: we allow extra partitions after partition ID 3
        return [
            self.parent_dev,
            "/dev/sda1",
            SLOT_A_DEV,
            SLOT_B_DEV,
            "/dev/sda5",
        ]

    def get_attrs_by_dev(self, _, dev, *args, **kwargs):
        if dev == self.active_slot_dev:
            return self.active_slot
        elif dev == self.standby_slot_dev:
            return self.standby_slot
        raise ValueError


class TestRPIBootControl:
    """
    Simulating otaclient starts from slot_a, and apply ota_update to slot_b
    """

    @pytest.fixture
    def rpi_boot_ab_slot(self, tmp_path: Path, ab_slots: SlotMeta):
        self.model_file = tmp_path / "model"
        self.model_file.write_text(rpi_boot_cfg.RPI_MODEL_HINT)

        self.slot_a_mp = Path(ab_slots.slot_a)
        self.slot_b_mp = Path(ab_slots.slot_b)

        # setup ota_status dir for slot_a
        self.slot_a_ota_status_dir = self.slot_a_mp / Path(
            rpi_boot_cfg.OTA_STATUS_DIR
        ).relative_to("/")
        self.slot_a_ota_status_dir.mkdir(parents=True, exist_ok=True)
        slot_a_ota_status = self.slot_a_ota_status_dir / "status"
        slot_a_ota_status.write_text(api_types.StatusOta.SUCCESS.name)
        slot_a_version = self.slot_a_ota_status_dir / "version"
        slot_a_version.write_text(cfg.CURRENT_VERSION)
        slot_a_slot_in_use = self.slot_a_ota_status_dir / "slot_in_use"
        slot_a_slot_in_use.write_text(SLOT_A)
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
        (self.system_boot / f"{_rpi_boot.CONFIG_TXT}").write_text(CONFIG_TXT_SLOT_A)
        # NOTE: rpi_boot controller now doesn't check the content of boot files, but only ensure the existence
        (self.system_boot / f"{_rpi_boot.CONFIG_TXT}{SEP_CHAR}{SLOT_A}").write_text(
            CONFIG_TXT_SLOT_A
        )
        (self.system_boot / f"{_rpi_boot.CONFIG_TXT}{SEP_CHAR}{SLOT_B}").write_text(
            CONFIG_TXT_SLOT_B
        )
        (self.system_boot / f"{_rpi_boot.CMDLINE_TXT}{SEP_CHAR}{SLOT_A}").write_text(
            CMDLINE_TXT_SLOT_A
        )
        (self.system_boot / f"{_rpi_boot.CMDLINE_TXT}{SEP_CHAR}{SLOT_B}").write_text(
            CMDLINE_TXT_SLOT_B
        )
        (self.system_boot / f"{_rpi_boot.VMLINUZ}{SEP_CHAR}{SLOT_A}").write_text(
            "slot_a_vmlinux"
        )
        (self.system_boot / f"{_rpi_boot.INITRD_IMG}{SEP_CHAR}{SLOT_A}").write_text(
            "slot_a_initrdimg"
        )
        self.vmlinuz_slot_b = (
            self.system_boot / f"{_rpi_boot.VMLINUZ}{SEP_CHAR}{SLOT_B}"
        )
        self.initrd_img_slot_b = (
            self.system_boot / f"{_rpi_boot.INITRD_IMG}{SEP_CHAR}{SLOT_B}"
        )

    @pytest.fixture(autouse=True)
    def mock_setup(self, mocker: pytest_mock.MockerFixture, rpi_boot_ab_slot):
        # start the test FSM
        self.fsm = fsm = RPIBootABPartitionFSM()

        # ------ patch CMDHelperFuncs ------ #
        # NOTE: also remember to patch CMDHelperFuncs in common
        self.CMDHelper_mock = CMDHelper_mock = typing.cast(
            CMDHelperFuncs, mocker.MagicMock(spec=CMDHelperFuncs)
        )
        # NOTE: this is for system-boot mount check in _RPIBootControl;
        CMDHelper_mock.is_target_mounted = mocker.Mock(return_value=True)
        CMDHelper_mock.get_current_rootfs_dev = mocker.Mock(
            wraps=fsm.get_current_rootfs_dev
        )
        CMDHelper_mock.get_parent_dev = mocker.Mock(wraps=fsm.get_parent_dev)
        CMDHelper_mock.get_device_tree = mocker.Mock(wraps=fsm.get_device_tree)
        CMDHelper_mock.get_attrs_by_dev = mocker.Mock(wraps=fsm.get_attrs_by_dev)

        mocker.patch(rpi_boot_RPIBoot_CMDHelperFuncs_MODULE, self.CMDHelper_mock)
        mocker.patch(
            boot_control_common_CMDHelperFuncs_MODULE,
            self.CMDHelper_mock,
        )

        # ------ patch _RPIBootControl ------ #
        def _update_firmware(*, target_slot, **kwargs):
            """Move the kernel and initrd images into /boot/firmware folder.

            This is done by update_firmware method. This simulates the update_firmware to slot_b.
            """
            assert target_slot == SLOT_B

            _vmlinuz = self.slot_a_mp / "boot" / _rpi_boot.VMLINUZ
            shutil.copy(
                os.path.realpath(_vmlinuz),
                self.system_boot / f"{_rpi_boot.VMLINUZ}{SEP_CHAR}{SLOT_B}",
            )
            _initrd_img = self.slot_a_mp / "boot" / _rpi_boot.INITRD_IMG
            shutil.copy(
                os.path.realpath(_initrd_img),
                self.system_boot / f"{_rpi_boot.INITRD_IMG}{SEP_CHAR}{SLOT_B}",
            )

        self.update_firmware_mock = update_firmware_mock = mocker.MagicMock(
            wraps=_update_firmware
        )
        mocker.patch(
            f"{rpi_boot__RPIBootControl_MODULE}.update_firmware", update_firmware_mock
        )
        self.reboot_tryboot_mock = reboot_tryboot_mock = mocker.MagicMock(
            side_effect=fsm.reboot_tryboot
        )
        mocker.patch(
            f"{rpi_boot__RPIBootControl_MODULE}.reboot_tryboot", reboot_tryboot_mock
        )

        # ------ patch slot mount helper ------ #
        self.mp_control_mock = mp_control_mock = typing.cast(
            SlotMountHelper, mocker.MagicMock()
        )

        def _get_active_slot_mount_point(*args, **kwargs):
            if fsm.active_slot == SLOT_A:
                return self.slot_a_mp
            elif fsm.active_slot == SLOT_B:
                return self.slot_b_mp

        def _get_standby_slot_mount_point(*args, **kwargs):
            if fsm.standby_slot == SLOT_A:
                return self.slot_a_mp
            elif fsm.standby_slot == SLOT_B:
                return self.slot_b_mp

        type(mp_control_mock).active_slot_mount_point = mocker.PropertyMock(  # type: ignore
            wraps=_get_active_slot_mount_point
        )
        type(mp_control_mock).standby_slot_mount_point = mocker.PropertyMock(  # type: ignore
            wraps=_get_standby_slot_mount_point
        )

        mocker.patch(
            f"{RPI_BOOT_MODULE_PATH}.SlotMountHelper",
            mocker.MagicMock(return_value=mp_control_mock),
        )

    def test_rpi_boot_normal_update(self, mocker: pytest_mock.MockerFixture):
        from otaclient.app.boot_control._rpi_boot import RPIBootController

        # ------ patch rpi_boot_cfg for boot_controller_inst1.stage 1~3 ------#
        rpi_boot_cfg_path = f"{cfg.BOOT_CONTROL_CONFIG_MODULE_PATH}.rpi_boot_cfg"
        mocker.patch(
            f"{rpi_boot_cfg_path}.SYSTEM_BOOT_MOUNT_POINT", str(self.system_boot)
        )
        mocker.patch(f"{rpi_boot_cfg_path}.ACTIVE_ROOTFS_PATH", str(self.slot_a_mp))
        mocker.patch(f"{rpi_boot_cfg_path}.MOUNT_POINT", str(self.slot_b_mp))
        mocker.patch(
            f"{rpi_boot_cfg_path}.ACTIVE_ROOT_MOUNT_POINT", str(self.slot_a_mp)
        )
        mocker.patch(f"{rpi_boot_cfg_path}.RPI_MODEL_FILE", str(self.model_file))

        # ------ boot_controller_inst1.stage1: init ------ #
        rpi_boot_controller1 = RPIBootController()

        # ------ boot_controller_inst1.stage2: pre_update ------ #
        rpi_boot_controller1.pre_update(
            version=VERSION,
            standby_as_ref=False,
            erase_standby=False,
        )

        # --- assertion --- #
        assert (
            self.slot_a_ota_status_dir / "status"
        ).read_text() == api_types.StatusOta.FAILURE.name
        assert (
            self.slot_b_ota_status_dir / "status"
        ).read_text() == api_types.StatusOta.UPDATING.name
        assert (
            (self.slot_a_ota_status_dir / "slot_in_use").read_text()
            == (self.slot_b_ota_status_dir / "slot_in_use").read_text()
            == SLOT_B
        )
        self.mp_control_mock.prepare_standby_dev.assert_called_once_with(
            erase_standby=mocker.ANY,
            fslabel=self.fsm.standby_slot,
        )
        self.mp_control_mock.mount_standby.assert_called_once()
        self.mp_control_mock.mount_active.assert_called_once()

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
        _post_updater = rpi_boot_controller1.post_update()
        next(_post_updater)
        next(_post_updater, None)  # actual reboot here

        # --- assertion --- #
        self.reboot_tryboot_mock.assert_called_once()
        self.update_firmware_mock.assert_called_once()
        assert self.fsm.is_switched_boot
        assert (
            self.slot_b_mp / Path(rpi_boot_cfg.FSTAB_FPATH).relative_to("/")
        ).read_text() == Template(_rpi_boot._FSTAB_TEMPLATE_STR).substitute(
            rootfs_fslabel=SLOT_B
        )
        assert self.initrd_img_slot_b.is_file()
        assert self.vmlinuz_slot_b.is_file()
        assert (self.system_boot / "tryboot.txt").read_text() == CONFIG_TXT_SLOT_B
        assert (self.system_boot / "config.txt").read_text() == CONFIG_TXT_SLOT_A
        # NOTE: backward compatibility with old double stage update strategy
        assert not (self.system_boot / rpi_boot_cfg.SWITCH_BOOT_FLAG_FILE).is_file()

        # ------ boot_controller_inst2: first reboot ------ #
        mocker.patch(f"{rpi_boot_cfg_path}.ACTIVE_ROOTFS_PATH", str(self.slot_b_mp))
        mocker.patch(f"{rpi_boot_cfg_path}.MOUNT_POINT", str(self.slot_a_mp))

        # ------ boot_controller_inst2.stage1: finalize switchboot ------ #
        logger.info("1st reboot: finalize switch boot and update firmware....")
        RPIBootController()

        # --- assertion --- #
        assert (self.system_boot / "config.txt").read_text() == CONFIG_TXT_SLOT_B
        assert (
            self.slot_b_ota_status_dir / rpi_boot_cfg.SLOT_IN_USE_FNAME
        ).read_text() == "slot_b"
        assert not (self.system_boot / rpi_boot_cfg.SWITCH_BOOT_FLAG_FILE).is_file()
        assert (
            self.slot_b_ota_status_dir / rpi_boot_cfg.OTA_STATUS_FNAME
        ).read_text() == api_types.StatusOta.SUCCESS.name
