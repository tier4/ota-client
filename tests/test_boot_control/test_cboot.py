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


from __future__ import annotations
import logging
import shutil
import typing
import pytest
import pytest_mock
from functools import partial
from pathlib import Path

from otaclient._utils.path import replace_root
from otaclient.app.proto import wrapper
from otaclient.app.boot_control._common import SlotMountHelper
from otaclient.app.boot_control._cboot import Nvbootctrl
from otaclient.configs.app_cfg import Config as otaclient_Config


from tests.utils import SlotMeta, compare_dir
from tests.conftest import TestConfiguration as test_cfg

logger = logging.getLogger(__name__)


class _CBootTestCFG:
    SLOT_A_ID = "0"
    SLOT_B_ID = "1"
    SLOT_A_DEV = "/dev/nvme0n1p1"
    SLOT_B_DEV = "/dev/nvme0n1p2"

    SLOT_A_UUID = "aaaaaaaa-0000-0000-0000-aaaaaaaaaaaa"
    SLOT_A_PARTUUID = SLOT_A_UUID
    SLOT_B_UUID = "bbbbbbbb-1111-1111-1111-bbbbbbbbbbbb"
    SLOT_B_PARTUUID = SLOT_B_UUID

    CBOOT_MODULE_PATH = "otaclient.app.boot_control._cboot"


class CbootFSM:
    def __init__(self) -> None:
        self.current_slot, self.current_slot_dev = (
            _CBootTestCFG.SLOT_A_ID,
            _CBootTestCFG.SLOT_A_DEV,
        )
        self.standby_slot, self.standby_slot_dev = (
            _CBootTestCFG.SLOT_B_ID,
            _CBootTestCFG.SLOT_B_DEV,
        )
        self.current_slot_bootable = True
        self.standby_slot_bootable = True

        self.is_boot_switched = False

    def get_current_slot(self, *args, **kwargs):
        return self.current_slot

    def get_current_slot_dev(self, *args, **kwargs):
        return self.current_slot_dev

    def get_standby_slot(self, *args, **kwargs):
        return self.standby_slot

    def get_standby_slot_dev(self, *args, **kwargs):
        return self.standby_slot_dev

    def get_standby_partuuid(self, *args, **kwargs):
        if self.standby_slot == _CBootTestCFG.SLOT_B_ID:
            return _CBootTestCFG.SLOT_B_PARTUUID
        return _CBootTestCFG.SLOT_A_PARTUUID

    def is_current_slot_bootable(self):
        return self.current_slot_bootable

    def is_standby_slot_bootable(self):
        return self.standby_slot_bootable

    def mark_current_slot_as(self, bootable: bool):
        self.current_slot_bootable = bootable

    def mark_standby_slot_as(self, bootable: bool):
        self.standby_slot_bootable = bootable

    def switch_boot_to(self, slot: str):
        assert slot == self.standby_slot

        self.current_slot, self.standby_slot = self.standby_slot, self.current_slot
        self.current_slot_dev, self.standby_slot_dev = (
            self.standby_slot_dev,
            self.current_slot_dev,
        )

        self.current_slot_bootable, self.standby_slot_bootable = (
            self.standby_slot_bootable,
            self.current_slot_bootable,
        )
        self.is_boot_switched = True


class TestCBootControl:
    EXTLNUX_CFG_SLOT_A = Path(__file__).parent / "extlinux.conf_slot_a"
    EXTLNUX_CFG_SLOT_B = Path(__file__).parent / "extlinux.conf_slot_b"

    @pytest.fixture
    def cboot_ab_slot(self, ab_slots: SlotMeta):
        """
        TODO: not considering rootfs on internal storage now
        """

        self.slot_a = Path(ab_slots.slot_a)
        self.slot_b = Path(ab_slots.slot_b)
        self.slot_a_boot_dev = Path(ab_slots.slot_a_boot_dev)
        self.slot_b_boot_dev = Path(ab_slots.slot_b_boot_dev)
        self.slot_a_uuid = test_cfg.SLOT_A_PARTUUID
        self.slot_b_uuid = test_cfg.SLOT_B_PARTUUID

        #
        # prepare ota_status dir for slot_a
        #
        self.slot_a_ota_status_dir = Path(
            replace_root(test_cfg.OTA_STATUS_DIR, "/", str(self.slot_a))
        )
        self.slot_a_ota_status_dir.mkdir(parents=True)

        slot_a_ota_status = self.slot_a_ota_status_dir / "status"
        slot_a_ota_status.write_text(wrapper.StatusOta.SUCCESS.name)

        slot_a_version = self.slot_a_ota_status_dir / "version"
        slot_a_version.write_text(test_cfg.CURRENT_VERSION)

        slot_a_slot_in_use = self.slot_a_ota_status_dir / "slot_in_use"
        slot_a_slot_in_use.write_text(_CBootTestCFG.SLOT_A_ID)

        #
        # prepare extlinux file for slot_a
        #
        extlinux_dir = self.slot_a / "boot/extlinux"
        extlinux_dir.mkdir(parents=True)
        extlinux_cfg = extlinux_dir / "extlinux.conf"
        extlinux_cfg.write_text(self.EXTLNUX_CFG_SLOT_A.read_text())

        # copy the /boot folder from slot_a to slot_a_boot_dev
        shutil.copytree(self.slot_a / "boot", self.slot_a_boot_dev, dirs_exist_ok=True)

        #
        # ota_status dir for slot_b(separate boot dev)
        #
        self.slot_b_ota_status_dir = Path(
            replace_root(test_cfg.OTA_STATUS_DIR, "/", str(self.slot_b))
        )

    @pytest.fixture(autouse=True)
    def mock_setup(
        self, mocker: pytest_mock.MockerFixture, cboot_ab_slot, patch_cmdhelper
    ):
        from otaclient.app.boot_control._cboot import _CBootControl

        ###### start fsm ######
        self._fsm = CbootFSM()

        # init config

        # config for slot_a as active rootfs
        self.mocked_cfg_slot_a = otaclient_Config(ACTIVE_ROOTFS=str(self.slot_a))
        mocker.patch(
            f"{test_cfg.BOOT_CONTROL_CONFIG_MODULE_PATH}.cfg", self.mocked_cfg_slot_a
        )
        mocker.patch(f"{_CBootTestCFG.CBOOT_MODULE_PATH}.cfg", self.mocked_cfg_slot_a)
        # NOTE: remember to also patch boot.common module
        mocker.patch(
            f"{test_cfg.BOOT_CONTROL_COMMON_MODULE_PATH}.cfg", self.mocked_cfg_slot_a
        )

        # config for slot_b as active rootfs
        self.mocked_cfg_slot_b = otaclient_Config(ACTIVE_ROOTFS=str(self.slot_b))

        #
        # ------ prepare mocked _CBootControl ------ #
        #

        _mocked__cboot_control_type = typing.cast(
            "type[_CBootControl]",
            type("_mocked__cboot_control", (_CBootControl,), {}),
        )

        # mock methods
        _mocked__cboot_control_type._check_tegra_chip_id = mocker.MagicMock()

        _mocked__cboot_control_type.active_slot = mocker.PropertyMock(  # type: ignore
            wraps=self._fsm.get_current_slot
        )
        _mocked__cboot_control_type.standby_slot = mocker.PropertyMock(  # type: ignore
            wraps=self._fsm.get_standby_slot
        )
        _mocked__cboot_control_type.standby_slot_dev_partuuid = mocker.PropertyMock(  # type: ignore
            wraps=self._fsm.get_standby_partuuid
        )
        _mocked__cboot_control_type.mark_current_slot_boot_successful = (
            mocker.MagicMock(side_effect=partial(self._fsm.mark_current_slot_as, True))
        )
        _mocked__cboot_control_type.set_standby_slot_unbootable = mocker.MagicMock(
            side_effect=partial(self._fsm.mark_standby_slot_as, False)
        )
        _mocked__cboot_control_type.switch_boot_to = mocker.MagicMock(
            side_effect=self._fsm.switch_boot_to
        )
        _mocked__cboot_control_type.is_current_slot_marked_successful = (
            mocker.MagicMock(wraps=self._fsm.is_current_slot_bootable)
        )
        # NOTE: we only test external rootfs
        _mocked__cboot_control_type.is_rootfs_on_external = mocker.PropertyMock(  # type: ignore
            return_value=True
        )

        # make update_extlinux_cfg as it
        _mocked__cboot_control_type.update_extlinux_cfg = mocker.MagicMock(
            wraps=_CBootControl.update_extlinux_cfg
        )

        # patch _CBootControl

        mocker.patch(
            f"{_CBootTestCFG.CBOOT_MODULE_PATH}._CBootControl",
            _mocked__cboot_control_type,
        )

        # patch standby slot preparing

        mocker.patch(
            f"{_CBootTestCFG.CBOOT_MODULE_PATH}.prepare_standby_slot_dev_ext4",
            mocker.MagicMock(),
        )

        # patch slot mount helper

        _mocked_slot_mount_helper_type = typing.cast(
            "type[SlotMountHelper]",
            type("_mocked_slot_mount_helper_type", (SlotMountHelper,), {}),
        )

        def _mount_standby_slot(*args, **kwargs):
            # remove the old already exists folder
            Path(self.mocked_cfg_slot_a.STANDBY_SLOT_MP).rmdir()
            # simulate mount slot_b into otaclient mount space on slot_a
            Path(self.mocked_cfg_slot_a.STANDBY_SLOT_MP).symlink_to(self.slot_b)

        def _mount_active_slot(*args, **kwargs):
            # remove the old already exists folder
            Path(self.mocked_cfg_slot_a.ACTIVE_SLOT_MP).rmdir()
            # simlulate mount slot_b into otaclient mount space on slot_a
            Path(self.mocked_cfg_slot_a.ACTIVE_SLOT_MP).symlink_to(self.slot_a)

        _mocked_slot_mount_helper_type.mount_active_slot_dev = mocker.MagicMock(
            wraps=_mount_active_slot
        )
        _mocked_slot_mount_helper_type.mount_standby_slot_dev = mocker.MagicMock(
            wraps=_mount_standby_slot
        )

        mocker.patch(
            f"{_CBootTestCFG.CBOOT_MODULE_PATH}.SlotMountHelper",
            _mocked_slot_mount_helper_type,
        )

        #
        # ------ patch nvboot ctrl helper class ------ #
        #

        _mocked_nvboot_ctrl_type = typing.cast(
            "type[Nvbootctrl]", type("_mocked_nvboot_ctrl", (Nvbootctrl,), {})
        )
        _mocked_nvboot_ctrl_type.get_current_slot = mocker.MagicMock(
            wraps=self._fsm.get_current_slot
        )

        mocker.patch(
            f"{_CBootTestCFG.CBOOT_MODULE_PATH}.Nvbootctrl", _mocked_nvboot_ctrl_type
        )

        #
        # ------ patch specific commands from cmdhelper module ------ #
        #

        # prevent subprocess call being executed
        mocker.patch(
            f"{_CBootTestCFG.CBOOT_MODULE_PATH}.subprocess_check_output",
            mocker.MagicMock(),
        )
        mocker.patch(
            f"{_CBootTestCFG.CBOOT_MODULE_PATH}.subprocess_call", mocker.MagicMock()
        )

        _mocked_get_current_rootfs_dev = mocker.MagicMock(
            wraps=self._fsm.get_current_slot_dev
        )
        mocker.patch(
            f"{_CBootTestCFG.CBOOT_MODULE_PATH}.get_current_rootfs_dev",
            _mocked_get_current_rootfs_dev,
        )

        _mocked_get_dev_partuuid = mocker.MagicMock(
            wraps=self._fsm.get_standby_partuuid
        )
        mocker.patch(
            f"{_CBootTestCFG.CBOOT_MODULE_PATH}.get_dev_partuuid",
            _mocked_get_dev_partuuid,
        )

        #
        # ------ patch reboot ------ #
        #
        # NOTE: if not patched, the reboot will call sys.exit(0)
        _mocked_reboot = mocker.MagicMock()
        mocker.patch(f"{_CBootTestCFG.CBOOT_MODULE_PATH}.reboot", _mocked_reboot)
        self._mocked_reboot = _mocked_reboot

        #
        # ------ binding mocked object to test instance #
        #
        self.__cbootcontrol_mock = _mocked__cboot_control_type

    def test_cboot_normal_update(self, mocker: pytest_mock.MockerFixture, mock_setup):
        from otaclient.app.boot_control._cboot import CBootController
        from otaclient.app.boot_control.configs import CBootControlConfig, cboot_cfg

        ###### stage 1 ######
        active_ota_status_dpath = Path(cboot_cfg.ACTIVE_BOOT_OTA_STATUS_DPATH)
        standby_ota_status_dpath = Path(cboot_cfg.STANDBY_BOOT_OTA_STATUS_DPATH)

        logger.info("[TESTING] init cboot controller...")
        cboot_controller = CBootController()
        assert (
            active_ota_status_dpath / otaclient_Config.OTA_STATUS_FNAME
        ).read_text() == wrapper.StatusOta.SUCCESS.name

        # test cboot pre-update
        cboot_controller.pre_update(
            version=test_cfg.UPDATE_VERSION,
            standby_as_ref=False,  # NOTE: not used
            erase_standby=False,  # NOTE: not used
        )
        # assert current slot ota-status
        assert (
            active_ota_status_dpath / otaclient_Config.OTA_STATUS_FNAME
        ).read_text() == wrapper.StatusOta.FAILURE.name

        assert (
            active_ota_status_dpath / otaclient_Config.SLOT_IN_USE_FNAME
        ).read_text() == self._fsm.get_standby_slot()

        # assert standby slot ota-status
        # NOTE: after pre_update phase, standby slot should be "mounted"
        assert (
            standby_ota_status_dpath / otaclient_Config.OTA_STATUS_FNAME
        ).read_text() == wrapper.StatusOta.UPDATING.name

        assert (
            standby_ota_status_dpath / otaclient_Config.OTA_VERSION_FNAME
        ).read_text() == test_cfg.UPDATE_VERSION

        assert (
            standby_ota_status_dpath / otaclient_Config.SLOT_IN_USE_FNAME
        ).read_text() == self._fsm.get_standby_slot()

        logger.info("[TESTING] pre-update completed, entering post-update...")
        # NOTE: standby slot's extlinux file is not yet populated(done by create_standby)
        #       prepare it by ourself
        # NOTE 2: populate to standby rootfs' boot folder
        standby_slot_extlinux_dir = Path(cboot_cfg.STANDBY_BOOT_EXTLINUX_DPATH)
        standby_slot_extlinux_dir.mkdir(exist_ok=True, parents=True)
        Path(cboot_cfg.STANDBY_EXTLINUX_FPATH).write_text(
            self.EXTLNUX_CFG_SLOT_A.read_text()
        )

        # Prepare the symlink from standby slot boot dev mountpoint to standby slot
        #   boot dev to simulate mounting.
        # This is for post_update copies boot folders from standby slot to standby boot dev.
        shutil.rmtree(cboot_cfg.SEPARATE_BOOT_MOUNT_POINT, ignore_errors=True)
        Path(cboot_cfg.SEPARATE_BOOT_MOUNT_POINT).symlink_to(self.slot_b_boot_dev)

        # test cboot post-update
        _post_updater = cboot_controller.post_update()
        next(_post_updater)
        next(_post_updater, None)

        #
        # assertion
        #
        # confirm the extlinux.conf is properly updated in standby slot
        assert (
            Path(cboot_cfg.STANDBY_EXTLINUX_FPATH).read_text()
            == self.EXTLNUX_CFG_SLOT_B.read_text()
        )

        # confirm extlinux.cfg for standby slot is properly copied
        # to standby slot's boot dev
        assert (
            Path(
                replace_root(
                    cboot_cfg.STANDBY_EXTLINUX_FPATH,
                    self.mocked_cfg_slot_a.STANDBY_SLOT_MP,
                    str(self.slot_b_boot_dev),
                )
            ).read_text()
            == self.EXTLNUX_CFG_SLOT_B.read_text()
        )

        self.__cbootcontrol_mock.switch_boot_to.assert_called_once()
        self._mocked_reboot.assert_called_once()

        # assert separate bootdev is populated correctly
        compare_dir(
            self.slot_b / "boot",
            self.slot_b_boot_dev / "boot",
        )

        ###### stage 2 ######

        logger.info("[TESTING] cboot init on first reboot...")

        # slot_b as active slot
        mocker.patch(
            f"{test_cfg.BOOT_CONTROL_CONFIG_MODULE_PATH}.cfg", self.mocked_cfg_slot_b
        )
        mocker.patch(f"{_CBootTestCFG.CBOOT_MODULE_PATH}.cfg", self.mocked_cfg_slot_b)

        # NOTE: old cboot_cfg's properties are cached, so create a new one
        _recreated_cboot_cfg = CBootControlConfig()
        mocker.patch(
            f"{_CBootTestCFG.CBOOT_MODULE_PATH}.boot_cfg", _recreated_cboot_cfg
        )

        # init cboot control again
        CBootController()
        assert (
            Path(_recreated_cboot_cfg.ACTIVE_BOOT_OTA_STATUS_DPATH)
            / otaclient_Config.OTA_STATUS_FNAME
        ).read_text() == wrapper.StatusOta.SUCCESS.name

        assert self._fsm.is_boot_switched
