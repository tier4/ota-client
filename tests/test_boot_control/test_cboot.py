r"""
CBOOT switch boot mechanism follow(normal successful case):
* Assume we are at slot-0, and apply an OTA update

condition before OTA update:
    current slot: 0, ota_status=SUCCESS, slot_in_use=0
    standby slot: 1

pre-update:
    1. store current ota_status(=FAILURE)
    2. store current slot_in_use(=1)
    3. set standby_slot unbootable
    4. prepare and mount standby(params: standby_rootfs_dev, erase_standby)
    5. mount refroot(params: standby_rootfs_dev, current_rootfs_dev, standby_as_ref)
    6. store standby ota_status(=UPDATING)
    7. store standby slot_in_use(=1), standby_version
post-update:
    1. update extlinux_cfg file
    2. (if external_rootfs_enabled) populate boot folder to bootdev
    3. umount all
    4. switch boot
first-reboot
init boot controller
    1. makr current slot boot successful

condition after OTA update:
    current slot: 1, ota_status=SUCCESS, slot_in_use=1
    standby slot: 0
"""
from functools import partial
from pathlib import Path
import typing
import pytest
import pytest_mock

from app.boot_control.common import CMDHelperFuncs

import logging

from tests.utils import SlotMeta

logger = logging.getLogger(__name__)


class CbootFSM:
    def __init__(self) -> None:
        self.current_slot = 0
        self.standby_slot = 1
        self.current_slot_bootable = True
        self.standby_slot_bootable = True

        self.is_boot_switched = False

    def mark_current_slot_as(self, bootable: bool):
        self.current_slot_bootable = bootable

    def mark_standby_slot_as(self, bootable: bool):
        self.standby_slot_bootable = bootable

    def switch_boot(self):
        self.current_slot, self.standby_slot = self.standby_slot, self.current_slot
        self.current_slot_bootable, self.standby_slot_bootable = (
            self.standby_slot_bootable,
            self.current_slot_bootable,
        )
        self.is_boot_switched = True


class TestCBootControl:
    """
    current slot: 0, ota_status=SUCCESS, slot_in_use=0
    standby slot: 1
    """

    CURRENT_VERSION = "123.x"
    UPDATE_VERSION = "789.x"
    EXTLNUX_CFG_SLOT_A = Path(__file__).parent / "extlinux.conf_slot_a"
    EXTLNUX_CFG_SLOT_B = Path(__file__).parent / "extlinux.conf_slot_a"

    @pytest.fixture
    def cboot_ab_slot(self, ab_slots: SlotMeta):
        """
        # TODO: not considering rootfs on internal storage now
        boot folder structure for cboot:
            boot_dir_{slot_a, slot_b}/
                ota-status/
                    status
                    version
                    slot_in_use
        """
        self.slot_a = Path(ab_slots.slot_a)
        self.slot_b = Path(ab_slots.slot_b)
        self.slot_a_boot_dir = Path(ab_slots.slot_a_boot_dir)
        self.slot_b_boot_dir = Path(ab_slots.slot_b_boot_dir)
        self.slot_a_partuuid = ab_slots.slot_a_partuuid
        self.slot_b_partuuid = ab_slots.slot_b_boot_dir

        self.current_slot = "0"
        self._fsm = CbootFSM()

        # prepare ota_status dir for slot_a
        self.slot_a_ota_status_dir = self.slot_a_boot_dir / "ota-status"
        self.slot_a_ota_status_dir.mkdir()
        slot_a_ota_status = self.slot_a_ota_status_dir / "status"
        slot_a_ota_status.write_text("SUCCESS")
        slot_a_version = self.slot_a_ota_status_dir / "version"
        slot_a_version.write_text(self.CURRENT_VERSION)
        slot_a_slot_in_use = self.slot_a_ota_status_dir / "slot_in_use"
        slot_a_slot_in_use.write_text("0")

        # prepare extlinux file
        extlinux_dir = self.slot_a_boot_dir / "extlinux"
        extlinux_dir.mkdir()
        extlinux_cfg = extlinux_dir / "extlinux.conf"
        extlinux_cfg.write_text(self.EXTLNUX_CFG_SLOT_A.read_text())

        # ota_status dir for slot_b
        self.slot_b_ota_status_dir = self.slot_b_boot_dir / "ota-status"
        self.slot_b_ota_status_dir.mkdir()

    @pytest.fixture
    def mock_cfg(self, mocker: pytest_mock.MockerFixture, cboot_ab_slot):
        from app.configs import CBootControlConfig

        mocker.patch("app.boot_control.cboot.BOOTLOADER", "cboot")
        _mocked_cboot_cfg = CBootControlConfig()
        _mocked_cboot_cfg.MOUNT_POINT = str(self.slot_b)
        _mocked_cboot_cfg.REF_ROOT_MOUNT_POINT = str(self.slot_a)
        _mocked_cboot_cfg.SEPARATE_BOOT_MOUNT_POINT = str(self.slot_b_boot_dir)
        _mocked_cboot_cfg.OTA_STATUS_DIR = str(self.slot_a_boot_dir / "ota-status")
        mocker.patch("app.boot_control.cboot.cfg", _mocked_cboot_cfg)

    @pytest.fixture(autouse=True)
    def mock_setup(
        self,
        mocker: pytest_mock.MockerFixture,
        mock_cfg,
        cboot_ab_slot,
    ):
        from app.boot_control.cboot import _CBootControl

        ###### mocking _CBootControl ######
        _CBootControl_mock = typing.cast(
            _CBootControl, mocker.MagicMock(spec=_CBootControl)
        )
        # mock methods
        _CBootControl_mock.get_current_slot.return_value = self._fsm.current_slot
        _CBootControl_mock.get_standby_slot.return_value = self._fsm.standby_slot
        _CBootControl_mock.mark_current_slot_boot_successful.side_effect = partial(
            self._fsm.mark_current_slot_as, True
        )
        _CBootControl_mock.set_standby_slot_unbootable.side_effect = partial(
            self._fsm.mark_standby_slot_as, False
        )
        _CBootControl_mock.switch_boot.side_effect = self._fsm.switch_boot
        _CBootControl_mock.is_current_slot_marked_successful.return_value = (
            self._fsm.current_slot_bootable
        )
        # NOTE: we only test external rootfs
        _CBootControl_mock.is_external_rootfs_enabled.return_value = True
        # make update_extlinux_cfg as it
        _CBootControl_mock.update_extlinux_cfg = _CBootControl.update_extlinux_cfg

        # patch the namespace
        _CBootControl_path = "app.boot_control.cboot._CBootControl"
        mocker.patch(_CBootControl_path, _CBootControl_mock)
        # also bind the cboot_ctrl mock to test instance for latter use
        self._CBootControl_mock = _CBootControl_mock

        ###### mocking _CMDHelper ######
        _CMDHelper_mock = typing.cast(
            CMDHelperFuncs, mocker.MagicMock(spec=CMDHelperFuncs)
        )
        _CMDHelper_path = "app.boot_control.common.CMDHelperFuncs"
        mocker.patch(_CMDHelper_path, _CMDHelper_mock)
        self._CMDHelper_mock = _CMDHelper_mock

        ###### mocking CBootController ######

    def test_cboot_normal_update(self, mocker: pytest_mock.MockerFixture):
        from app.boot_control.cboot import CBootController

        cboot_controller = CBootController()
        # TODO: assert normal init

        # test pre-update
        cboot_controller.pre_update(
            version=self.UPDATE_VERSION,
            standby_as_ref=False,  # NOTE: not used
            erase_standby=False,  # NOTE: not used
        )
        # TODO: assert pre-update ota-status

        logger.info("pre-update completed, entering post-update...")
        # test post-update
        cboot_controller.post_update()
        # TODO: assert post-update
        # TODO: assert extlinux file is updated
        # TODO: assert separate bootdev is populated
        assert self._CBootControl_mock.switch_boot.assert_called_once()
        assert self._CMDHelper_mock.reboot.assert_called_once()

        logger.info("post-update completed, test init after first reboot...")
        cboot_controller = CBootController()
        # TODO: assert ota-status to be SUCCESS
        # TODO: assert fsm status to be switched
        assert self._fsm.is_boot_switched
