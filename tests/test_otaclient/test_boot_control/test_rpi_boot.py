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


import logging
import os
import shutil
import typing
from pathlib import Path
from string import Template
from typing import Any

import pytest
import pytest_mock

from otaclient._types import OTAStatus
from otaclient.boot_control import _rpi_boot
from otaclient.boot_control._rpi_boot import RPIBootController
from otaclient.boot_control._slot_mnt_helper import SlotMountHelper
from otaclient.boot_control.configs import RPIBootControlConfig, rpi_boot_cfg
from otaclient.configs import DefaultOTAClientConfigs
from otaclient.configs.cfg import cfg as otaclient_cfg
from tests.conftest import TestConfiguration as cfg
from tests.utils import SlotMeta

logger = logging.getLogger(__name__)

MODULE = _rpi_boot.__name__

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
RPI_BOOT_MODULE_PATH = _rpi_boot.__name__
rpi_boot__RPIBootControl_MODULE = f"{RPI_BOOT_MODULE_PATH}._RPIBootControl"
rpi_boot_RPIBoot_cmdhelper_MODULE = f"{RPI_BOOT_MODULE_PATH}.cmdhelper"

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

    def reboot_tryboot(self, *args, **kwags):
        logger.info(f"tryboot to {self.standby_slot=}")
        self.is_switched_boot = True
        self.active_slot, self.standby_slot = self.standby_slot, self.active_slot
        self.active_slot_dev, self.standby_slot_dev = (
            self.standby_slot_dev,
            self.active_slot_dev,
        )

    def get_current_rootfs_dev(self, *args, **kwags):
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
        slot_a_ota_status.write_text(OTAStatus.SUCCESS)
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

        # ------ patch cmdhelper ------ #
        self.cmdhelper_mock = cmdhelper_mock = mocker.MagicMock()
        # NOTE: this is for system-boot mount check in _RPIBootControl;
        cmdhelper_mock.is_target_mounted = mocker.Mock(return_value=True)
        cmdhelper_mock.get_current_rootfs_dev = mocker.Mock(
            wraps=fsm.get_current_rootfs_dev
        )
        cmdhelper_mock.get_parent_dev = mocker.Mock(wraps=fsm.get_parent_dev)
        cmdhelper_mock.get_device_tree = mocker.Mock(wraps=fsm.get_device_tree)
        cmdhelper_mock.get_attrs_by_dev = mocker.Mock(wraps=fsm.get_attrs_by_dev)

        mocker.patch(rpi_boot_RPIBoot_cmdhelper_MODULE, self.cmdhelper_mock)

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
        # ------ patch rpi_boot_cfg for boot_controller_inst1.stage 1~3 ------#
        _mock_rpi_boot_cfg = RPIBootControlConfig()
        _mock_rpi_boot_cfg.SYSTEM_BOOT_MOUNT_POINT = str(self.system_boot)  # type: ignore[assignment]
        _mock_rpi_boot_cfg.RPI_MODEL_FILE = str(self.model_file)  # type: ignore[assignment]
        mocker.patch(f"{MODULE}.boot_cfg", _mock_rpi_boot_cfg)

        _mock_otaclient_cfg = DefaultOTAClientConfigs()
        _mock_otaclient_cfg.ACTIVE_ROOT = str(self.slot_a_mp)  # type: ignore[assignment]
        _mock_otaclient_cfg.STANDBY_SLOT_MNT = str(self.slot_b_mp)  # type: ignore[assignment]
        _mock_otaclient_cfg.ACTIVE_SLOT_MNT = str(self.slot_a_mp)  # type: ignore[assignment]
        mocker.patch(f"{MODULE}.cfg", _mock_otaclient_cfg)

        # ------ boot_controller_inst1.stage1: init ------ #
        rpi_boot_controller = RPIBootController()

        # ------ boot_controller_inst1.stage2: pre_update ------ #
        rpi_boot_controller.pre_update(
            standby_as_ref=False,
            erase_standby=False,
        )

        # --- assertion --- #
        assert (self.slot_a_ota_status_dir / "status").read_text() == OTAStatus.FAILURE
        assert (self.slot_a_ota_status_dir / "slot_in_use").read_text() == SLOT_B
        self.mp_control_mock.prepare_standby_dev.assert_called_once_with(  # type: ignore
            erase_standby=mocker.ANY,
            fslabel=self.fsm.standby_slot,
        )
        self.mp_control_mock.mount_standby.assert_called_once()  # type: ignore
        self.mp_control_mock.mount_active.assert_called_once()  # type: ignore

        # ------ mocked in_update ------ #
        # this should be done by create_standby module, so we do it manually here instead
        self.slot_b_boot_dir = self.slot_b_mp / "boot"
        self.slot_a_boot_dir = self.slot_a_mp / "boot"
        self.slot_a_boot_dir.mkdir(exist_ok=True, parents=True)
        self.slot_b_boot_dir.mkdir(exist_ok=True, parents=True)

        # NOTE: copy slot_a's kernel and initrd.img to slot_b,
        #       because we skip the create_standby step
        # NOTE 2: not copy the symlinks
        _vmlinuz = self.slot_a_boot_dir / "vmlinuz"
        shutil.copy(os.path.realpath(_vmlinuz), self.slot_b_boot_dir)
        _initrd_img = self.slot_a_boot_dir / "initrd.img"
        shutil.copy(os.path.realpath(_initrd_img), self.slot_b_boot_dir)

        # ------ boot_controller_inst1.stage3: post_update, reboot switch boot ------ #
        rpi_boot_controller: Any  # for typing only
        rpi_boot_controller.post_update(update_version=VERSION)
        rpi_boot_controller.finalizing_update()

        # --- assertion --- #
        assert (self.slot_b_ota_status_dir / "status").read_text() == OTAStatus.UPDATING
        assert (self.slot_b_ota_status_dir / "slot_in_use").read_text()
        self.reboot_tryboot_mock.assert_called_once()
        self.update_firmware_mock.assert_called_once()
        assert self.fsm.is_switched_boot
        assert (
            self.slot_b_mp / Path(otaclient_cfg.FSTAB_FPATH).relative_to("/")
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
        _mock_otaclient_cfg = DefaultOTAClientConfigs()
        _mock_otaclient_cfg.ACTIVE_ROOT = str(self.slot_b_mp)  # type: ignore[assignment]
        _mock_otaclient_cfg.STANDBY_SLOT_MNT = str(self.slot_a_mp)  # type: ignore[assignment]
        _mock_otaclient_cfg.ACTIVE_SLOT_MNT = str(self.slot_b_mp)  # type: ignore[assignment]
        mocker.patch(f"{MODULE}.cfg", _mock_otaclient_cfg)

        # ------ boot_controller_inst2.stage1: finalize switchboot ------ #
        logger.info("1st reboot: finalize switch boot and update firmware....")
        RPIBootController()

        # --- assertion --- #
        assert (self.system_boot / "config.txt").read_text() == CONFIG_TXT_SLOT_B
        assert (
            self.slot_b_ota_status_dir / otaclient_cfg.SLOT_IN_USE_FNAME
        ).read_text() == "slot_b"
        assert not (self.system_boot / rpi_boot_cfg.SWITCH_BOOT_FLAG_FILE).is_file()
        assert (
            self.slot_b_ota_status_dir / otaclient_cfg.OTA_STATUS_FNAME
        ).read_text() == OTAStatus.SUCCESS
