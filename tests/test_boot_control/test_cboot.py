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
import shutil
import typing
import pytest
import pytest_mock

import logging

from tests.utils import SlotMeta, compare_dir
from tests.conftest import TestConfiguration as cfg

logger = logging.getLogger(__name__)


class CbootFSM:
    def __init__(self) -> None:
        self.current_slot = cfg.SLOT_A_ID_CBOOT
        self.standby_slot = cfg.SLOT_B_ID_CBOOT
        self.current_slot_bootable = True
        self.standby_slot_bootable = True

        self.is_boot_switched = False

    def get_current_slot(self):
        return self.current_slot

    def get_standby_slot(self):
        return self.standby_slot

    def get_standby_partuuid_str(self):
        if self.standby_slot == cfg.SLOT_B_ID_CBOOT:
            return f"PARTUUID={cfg.SLOT_B_PARTUUID}"
        else:
            return f"PARTUUID={cfg.SLOT_A_PARTUUID}"

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

    def cfg_for_slot_a_as_current(self):
        """
        NOTE: we always only refer to ota-status dir at the rootfs!
        """
        from otaclient.app.boot_control.configs import CBootControlConfig

        _mocked_cboot_cfg = CBootControlConfig()
        _mocked_cboot_cfg.MOUNT_POINT = str(self.slot_b)  # type: ignore
        _mocked_cboot_cfg.ACTIVE_ROOT_MOUNT_POINT = str(self.slot_a)  # type: ignore
        # NOTE: SEPARATE_BOOT_MOUNT_POINT is the root of the boot device!
        _mocked_cboot_cfg.SEPARATE_BOOT_MOUNT_POINT = str(self.slot_b_boot_dev)
        _mocked_cboot_cfg.ACTIVE_ROOTFS_PATH = str(self.slot_a)  # type: ignore
        return _mocked_cboot_cfg

    def cfg_for_slot_b_as_current(self):
        from otaclient.app.boot_control.configs import CBootControlConfig

        _mocked_cboot_cfg = CBootControlConfig()
        _mocked_cboot_cfg.MOUNT_POINT = str(self.slot_a)  # type: ignore
        _mocked_cboot_cfg.ACTIVE_ROOT_MOUNT_POINT = str(self.slot_b)  # type: ignore
        # NOTE: SEPARATE_BOOT_MOUNT_POINT is the root of the boot device!
        _mocked_cboot_cfg.SEPARATE_BOOT_MOUNT_POINT = str(self.slot_a_boot_dev)
        _mocked_cboot_cfg.ACTIVE_ROOTFS_PATH = str(self.slot_b)  # type: ignore
        return _mocked_cboot_cfg

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
        self.slot_a_uuid = cfg.SLOT_A_PARTUUID
        self.slot_b_uuid = cfg.SLOT_B_PARTUUID

        # prepare ota_status dir for slot_a
        self.slot_a_ota_status_dir = self.slot_a / Path(cfg.OTA_STATUS_DIR).relative_to(
            "/"
        )
        self.slot_a_ota_status_dir.mkdir(parents=True)
        slot_a_ota_status = self.slot_a_ota_status_dir / "status"
        slot_a_ota_status.write_text("SUCCESS")
        slot_a_version = self.slot_a_ota_status_dir / "version"
        slot_a_version.write_text(cfg.CURRENT_VERSION)
        slot_a_slot_in_use = self.slot_a_ota_status_dir / "slot_in_use"
        slot_a_slot_in_use.write_text(cfg.SLOT_A_ID_CBOOT)
        # also prepare a copy of boot folder to rootfs
        shutil.copytree(
            self.slot_a_boot_dev / Path(cfg.BOOT_DIR).relative_to("/"),
            self.slot_a / Path(cfg.BOOT_DIR).relative_to("/"),
            dirs_exist_ok=True,
        )

        # prepare extlinux file
        extlinux_dir = self.slot_a / "boot/extlinux"
        extlinux_dir.mkdir()
        extlinux_cfg = extlinux_dir / "extlinux.conf"
        extlinux_cfg.write_text(self.EXTLNUX_CFG_SLOT_A.read_text())

        # ota_status dir for slot_b(separate boot dev)
        self.slot_b_ota_status_dir = self.slot_b / Path(cfg.OTA_STATUS_DIR).relative_to(
            "/"
        )

    @pytest.fixture
    def mock_setup(
        self,
        mocker: pytest_mock.MockerFixture,
        cboot_ab_slot,
    ):
        from otaclient.app.boot_control._cboot import _CBootControl
        from otaclient.app.boot_control._common import CMDHelperFuncs

        ###### start fsm ######
        self._fsm = CbootFSM()

        ###### mocking _CBootControl ######
        _CBootControl_mock = typing.cast(
            _CBootControl, mocker.MagicMock(spec=_CBootControl)
        )
        # mock methods
        _CBootControl_mock.get_current_slot = mocker.MagicMock(
            wraps=self._fsm.get_current_slot
        )
        _CBootControl_mock.get_standby_slot = mocker.MagicMock(
            wraps=self._fsm.get_standby_slot
        )
        _CBootControl_mock.get_standby_rootfs_partuuid_str = mocker.MagicMock(
            wraps=self._fsm.get_standby_partuuid_str
        )
        _CBootControl_mock.mark_current_slot_boot_successful.side_effect = partial(
            self._fsm.mark_current_slot_as, True
        )
        _CBootControl_mock.set_standby_slot_unbootable.side_effect = partial(
            self._fsm.mark_standby_slot_as, False
        )
        _CBootControl_mock.switch_boot.side_effect = self._fsm.switch_boot
        _CBootControl_mock.is_current_slot_marked_successful = mocker.MagicMock(
            wraps=self._fsm.is_current_slot_bootable
        )
        # NOTE: we only test external rootfs
        _CBootControl_mock.is_external_rootfs_enabled.return_value = True
        # make update_extlinux_cfg as it
        _CBootControl_mock.update_extlinux_cfg = mocker.MagicMock(
            wraps=_CBootControl.update_extlinux_cfg
        )

        ###### mocking _CMDHelper ######
        _CMDHelper_mock = typing.cast(
            CMDHelperFuncs, mocker.MagicMock(spec=CMDHelperFuncs)
        )

        ###### patching ######
        # patch _CBootControl
        _CBootControl_path = f"{cfg.CBOOT_MODULE_PATH}._CBootControl"
        mocker.patch(_CBootControl_path, return_value=_CBootControl_mock)
        # patch CMDHelperFuncs
        # NOTE: also remember to patch CMDHelperFuncs in common
        mocker.patch(f"{cfg.CBOOT_MODULE_PATH}.CMDHelperFuncs", _CMDHelper_mock)
        mocker.patch(
            f"{cfg.BOOT_CONTROL_COMMON_MODULE_PATH}.CMDHelperFuncs", _CMDHelper_mock
        )

        ###### binding mocked object to test instance ######
        self._CBootControl_mock = _CBootControl_mock
        self._CMDHelper_mock = _CMDHelper_mock

    def test_cboot_normal_update(self, mocker: pytest_mock.MockerFixture, mock_setup):
        from otaclient.app.boot_control._cboot import CBootController

        _cfg_patch_path = f"{cfg.CBOOT_MODULE_PATH}.cfg"
        _relative_ota_status_path = Path(cfg.OTA_STATUS_DIR).relative_to("/")

        ###### stage 1 ######
        mocker.patch(_cfg_patch_path, self.cfg_for_slot_a_as_current())
        logger.info("init cboot controller...")
        cboot_controller = CBootController()
        assert (
            self.slot_a / _relative_ota_status_path / "status"
        ).read_text() == "SUCCESS"

        # test pre-update
        cboot_controller.pre_update(
            version=cfg.UPDATE_VERSION,
            standby_as_ref=False,  # NOTE: not used
            erase_standby=False,  # NOTE: not used
        )
        # assert current slot ota-status
        assert (
            self.slot_a / _relative_ota_status_path / "status"
        ).read_text() == "FAILURE"
        assert (
            self.slot_a / "boot/ota-status/slot_in_use"
        ).read_text() == self._fsm.get_standby_slot()
        # assert standby slot ota-status
        assert (
            self.slot_b / _relative_ota_status_path / "status"
        ).read_text() == "UPDATING"
        assert (
            self.slot_b / _relative_ota_status_path / "version"
        ).read_text() == cfg.UPDATE_VERSION
        assert (
            self.slot_b / _relative_ota_status_path / "slot_in_use"
        ).read_text() == self._fsm.get_standby_slot()

        logger.info("pre-update completed, entering post-update...")
        # NOTE: standby slot's extlinux file is not yet populated(done by create_standby)
        #       prepare it by ourself
        # NOTE 2: populate to standby rootfs' boot folder
        standby_extlinux_dir = self.slot_b / "boot/extlinux"
        standby_extlinux_dir.mkdir()
        standby_extlinux_file = standby_extlinux_dir / "extlinux.conf"
        standby_extlinux_file.write_text(self.EXTLNUX_CFG_SLOT_A.read_text())

        # test post-update
        _post_updater = cboot_controller.post_update()
        next(_post_updater)
        next(_post_updater, None)
        assert (
            self.slot_b_boot_dev / "boot/extlinux/extlinux.conf"
        ).read_text() == self.EXTLNUX_CFG_SLOT_B.read_text()
        self._CBootControl_mock.switch_boot.assert_called_once()
        self._CMDHelper_mock.reboot.assert_called_once()
        # assert separate bootdev is populated correctly
        compare_dir(self.slot_b / "boot", self.slot_b_boot_dev / "boot")

        ###### stage 2 ######
        logger.info("post-update completed, test init after first reboot...")
        mocker.patch(_cfg_patch_path, self.cfg_for_slot_b_as_current())
        cboot_controller = CBootController()
        assert (
            self.slot_b / _relative_ota_status_path / "status"
        ).read_text() == "SUCCESS"
        assert self._fsm.is_boot_switched
