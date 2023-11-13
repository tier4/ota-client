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


from functools import partial
from pathlib import Path
import typing
import pytest
import pytest_mock

import logging
from otaclient._utils.path import replace_root

from otaclient.app.configs import Config as otaclient_Config
from otaclient.app.proto import wrapper

from tests.utils import SlotMeta, compare_dir
from tests.conftest import TestConfiguration as test_cfg

logger = logging.getLogger(__name__)


class CbootFSM:
    def __init__(self) -> None:
        self.current_slot = test_cfg.SLOT_A_ID_CBOOT
        self.standby_slot = test_cfg.SLOT_B_ID_CBOOT
        self.current_slot_bootable = True
        self.standby_slot_bootable = True

        self.is_boot_switched = False

    def get_current_slot(self):
        return self.current_slot

    def get_standby_slot(self):
        return self.standby_slot

    def get_standby_partuuid_str(self):
        if self.standby_slot == test_cfg.SLOT_B_ID_CBOOT:
            return f"PARTUUID={test_cfg.SLOT_B_PARTUUID}"
        else:
            return f"PARTUUID={test_cfg.SLOT_A_PARTUUID}"

    def is_current_slot_bootable(self):
        return self.current_slot_bootable

    def is_standby_slot_bootable(self):
        return self.standby_slot_bootable

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
    EXTLNUX_CFG_SLOT_A = Path(__file__).parent / "extlinux.conf_slot_a"
    EXTLNUX_CFG_SLOT_B = Path(__file__).parent / "extlinux.conf_slot_b"

    @pytest.fixture
    def cboot_ab_slot(self, ab_slots: SlotMeta):
        """
        TODO: not considering rootfs on internal storage now

        boot folder structure for cboot:
            boot_dir_{slot_a, slot_b}/
                ota-status/
                    status
                    version
                    slot_in_use
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
        slot_a_slot_in_use.write_text(test_cfg.SLOT_A_ID_CBOOT)

        #
        # symlink boot_dev/boot to slot_a/boot
        #
        # NOTE: this is reserved comparing to real condition,
        #       just for test convenience.
        (self.slot_a_boot_dev / "boot").symlink_to(
            self.slot_a / "boot", target_is_directory=True
        )

        #
        # prepare extlinux file for slot_a
        #
        extlinux_dir = self.slot_a / "boot/extlinux"
        extlinux_dir.mkdir(parents=True)
        extlinux_cfg = extlinux_dir / "extlinux.conf"
        extlinux_cfg.write_text(self.EXTLNUX_CFG_SLOT_A.read_text())

        #
        # ota_status dir for slot_b(separate boot dev)
        #
        self.slot_b_ota_status_dir = Path(
            replace_root(test_cfg.OTA_STATUS_DIR, "/", str(self.slot_b))
        )

    @pytest.fixture
    def mock_setup(self, mocker: pytest_mock.MockerFixture, cboot_ab_slot):
        from otaclient.app.boot_control._cboot import _CBootControl
        from otaclient.app.boot_control._common import CMDHelperFuncs

        ###### start fsm ######
        self._fsm = CbootFSM()

        # init config

        # config for slot_a as active rootfs
        self.mocked_cfg_slot_a = otaclient_Config(ACTIVE_ROOTFS=str(self.slot_a))
        mocker.patch(
            f"{test_cfg.BOOT_CONTROL_CONFIG_MODULE_PATH}.cfg", self.mocked_cfg_slot_a
        )
        mocker.patch(f"{test_cfg.CBOOT_MODULE_PATH}.cfg", self.mocked_cfg_slot_a)

        # config for slot_b as active rootfs
        self.mocked_cfg_slot_b = otaclient_Config(ACTIVE_ROOTFS=str(self.slot_b))

        # mocking _CBootControl

        __cbootcontrol_mock: _CBootControl = typing.cast(
            _CBootControl, mocker.MagicMock(spec=_CBootControl)
        )
        # mock methods
        __cbootcontrol_mock.get_current_slot = mocker.MagicMock(
            wraps=self._fsm.get_current_slot
        )
        __cbootcontrol_mock.get_standby_slot = mocker.MagicMock(
            wraps=self._fsm.get_standby_slot
        )
        __cbootcontrol_mock.get_standby_rootfs_partuuid_str = mocker.MagicMock(
            wraps=self._fsm.get_standby_partuuid_str
        )
        __cbootcontrol_mock.mark_current_slot_boot_successful.side_effect = partial(
            self._fsm.mark_current_slot_as, True
        )
        __cbootcontrol_mock.set_standby_slot_unbootable.side_effect = partial(
            self._fsm.mark_standby_slot_as, False
        )
        __cbootcontrol_mock.switch_boot.side_effect = self._fsm.switch_boot
        __cbootcontrol_mock.is_current_slot_marked_successful = mocker.MagicMock(
            wraps=self._fsm.is_current_slot_bootable
        )
        # NOTE: we only test external rootfs
        __cbootcontrol_mock.is_external_rootfs_enabled.return_value = True
        # make update_extlinux_cfg as it
        __cbootcontrol_mock.update_extlinux_cfg = mocker.MagicMock(
            wraps=_CBootControl.update_extlinux_cfg
        )

        ###### mocking _CMDHelper ######
        # mock all helper methods in CMDHelper

        _cmdhelper_mock = typing.cast(
            CMDHelperFuncs, mocker.MagicMock(spec=CMDHelperFuncs)
        )

        # patch _CBootControl

        mocker.patch(
            f"{test_cfg.CBOOT_MODULE_PATH}._CBootControl",
            return_value=__cbootcontrol_mock,
        )

        # patch CMDHelperFuncs

        # NOTE: also remember to patch CMDHelperFuncs in common
        mocker.patch(f"{test_cfg.CBOOT_MODULE_PATH}.CMDHelperFuncs", _cmdhelper_mock)
        mocker.patch(
            f"{test_cfg.BOOT_CONTROL_COMMON_MODULE_PATH}.CMDHelperFuncs",
            _cmdhelper_mock,
        )
        mocker.patch(f"{test_cfg.CBOOT_MODULE_PATH}.Nvbootctrl")

        # patch CBootControl mounting

        def _mount_standby_slot():
            # simulate mount slot_b into otaclient mount space on slot_a
            Path(self.mocked_cfg_slot_a.STANDBY_SLOT_MP).symlink_to(self.slot_b)

        def _mount_active_slot():
            Path(self.mocked_cfg_slot_a.ACTIVE_SLOT_MP).symlink_to(self.slot_a)

        mocker.patch(
            f"{test_cfg.CBOOT_MODULE_PATH}.CBootController",
            "_prepare_and_mount_standby",
            mocker.MagicMock(side_effect=_mount_standby_slot),
        )
        mocker.patch(
            f"{test_cfg.CBOOT_MODULE_PATH}.CBootController",
            "_mount_refroot",
            mocker.MagicMock(side_effect=_mount_active_slot),
        )

        ###### binding mocked object to test instance ######
        self.__cbootcontrol_mock = __cbootcontrol_mock
        self._cmdhelper_mock = _cmdhelper_mock

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
            active_ota_status_dpath / otaclient_Config.OTA_STATUS_FNAME
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
        Path(cboot_cfg.STANDBY_EXTLINUX_FPATH).write_text(
            self.EXTLNUX_CFG_SLOT_A.read_text()
        )

        # test cboot post-update
        _post_updater = cboot_controller.post_update()
        next(_post_updater)
        next(_post_updater, None)

        #
        # assertion
        #
        # confirm extlinux.cfg is properly prepared
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

        self.__cbootcontrol_mock.switch_boot.assert_called_once()
        self._cmdhelper_mock.reboot.assert_called_once()

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
        mocker.patch(f"{test_cfg.CBOOT_MODULE_PATH}.cfg", self.mocked_cfg_slot_b)

        # NOTE: old cboot_cfg's properties are cached, so create a new one
        _recreated_cboot_cfg = CBootControlConfig()
        mocker.patch(f"{test_cfg.CBOOT_MODULE_PATH}.boot_cfg", _recreated_cboot_cfg)

        # init cboot control again
        CBootController()
        assert (
            Path(
                _recreated_cboot_cfg.ACTIVE_BOOT_OTA_STATUS_DPATH
                / otaclient_Config.OTA_STATUS_FNAME
            ).read_text()
            == wrapper.StatusOta.SUCCESS.name
        )

        assert self._fsm.is_boot_switched
