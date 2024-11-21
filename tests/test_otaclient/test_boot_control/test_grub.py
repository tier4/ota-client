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


from __future__ import annotations

import logging
import os
import shutil
import typing
from pathlib import Path
from typing import Any

import pytest
import pytest_mock

from otaclient._types import OTAStatus
from otaclient.boot_control import _grub
from otaclient.boot_control.configs import GrubControlConfig
from otaclient.configs import DefaultOTAClientConfigs
from tests.conftest import TestConfiguration as cfg
from tests.utils import SlotMeta

logger = logging.getLogger(__name__)

MODULE = _grub.__name__


class GrubFSM:
    def __init__(self, slot_a_mp, slot_b_mp) -> None:
        self._current_slot = cfg.SLOT_A_ID_GRUB
        self._standby_slot = cfg.SLOT_B_ID_GRUB
        self._current_slot_mp = Path(slot_a_mp)
        self._standby_slot_mp = Path(slot_b_mp)
        self._current_slot_dev_uuid = cfg.SLOT_A_UUID
        self._standby_slot_dev_uuid = cfg.SLOT_B_UUID
        self.current_slot_bootable = True
        self.standby_slot_bootable = True

        self.is_boot_switched = False

    def get_active_slot(self) -> str:
        return self._current_slot

    def get_standby_slot(self) -> str:
        return self._standby_slot

    def get_active_slot_dev(self) -> str:
        return f"/dev/{self._current_slot}"

    def get_standby_slot_dev(self) -> str:
        return f"/dev/{self._standby_slot}"

    def get_active_slot_mp(self) -> Path:
        return self._current_slot_mp

    def get_standby_slot_mp(self) -> Path:
        return self._standby_slot_mp

    def get_standby_boot_dir(self) -> Path:
        return self._standby_slot_mp / "boot"

    def get_attrs_by_dev(self, attr: str, dev: str) -> str:
        if dev == self.get_standby_slot_dev():
            return self._standby_slot_dev_uuid
        else:
            return self._current_slot_dev_uuid

    def switch_boot(self):
        self._current_slot, self._standby_slot = self._standby_slot, self._current_slot
        self._current_slot_mp, self._standby_slot_mp = (
            self._standby_slot_mp,
            self._current_slot_mp,
        )
        self._current_slot_dev_uuid, self._standby_slot_dev_uuid = (
            self._standby_slot_dev_uuid,
            self._current_slot_dev_uuid,
        )
        self.is_boot_switched = True

    def cat_proc_cmdline(self):
        if self._current_slot == cfg.SLOT_A_ID_GRUB:
            return cfg.CMDLINE_SLOT_A
        else:
            return cfg.CMDLINE_SLOT_B


class GrubMkConfigFSM:
    # generated on non-ota-partition system
    GRUB_CFG_SLOT_A_NON_OTAPARTITION = (
        Path(__file__).parent / "grub.cfg_slot_a_non_otapartition"
    ).read_text()
    # (slot_a as active) generated and not yet updated on ota-partition system
    GRUB_CFG_SLOT_A_GENERATED = (Path(__file__).parent / "grub.cfg_slot_a").read_text()
    # (slot_a as active) ota standby rootfs updated
    GRUB_CFG_SLOT_A_UPDATED = (
        Path(__file__).parent / "grub.cfg_slot_a_updated"
    ).read_text()
    # (slot_b as active) generated and not yet updated on ota-partition system
    GRUB_CFG_SLOT_B_GENERATED = (Path(__file__).parent / "grub.cfg_slot_b").read_text()
    # (slot_b as active) ota standby rootfs updated
    GRUB_CFG_SLOT_B_UPDATED = (
        Path(__file__).parent / "grub.cfg_slot_b_updated"
    ).read_text()

    _MAPPING = {
        0: GRUB_CFG_SLOT_A_GENERATED,  # slot_a init1
        1: GRUB_CFG_SLOT_A_GENERATED,  # slot_a init2
        #
        2: GRUB_CFG_SLOT_A_GENERATED,  # slot_a post_update1
        3: GRUB_CFG_SLOT_A_GENERATED,  # slot_a post_update2
        #
        4: GRUB_CFG_SLOT_B_GENERATED,  # slot_b pre_init
        5: GRUB_CFG_SLOT_B_GENERATED,
    }

    def __init__(self) -> None:
        self._count = 0

    def grub_mkconfig(self):
        logger.info(f"grub_mkconfig called: #{self._count}")
        res = self._MAPPING[self._count]
        self._count += 1
        return res


class TestGrubControl:
    FSTAB_ORIGIN = (Path(__file__).parent / "fstab_origin").read_text()
    FSTAB_UPDATED = (Path(__file__).parent / "fstab_updated").read_text()
    DEFAULT_GRUB = (Path(__file__).parent / "default_grub").read_text()

    @pytest.fixture(scope="class")
    def _grub_mkconfig_fsm(self) -> GrubMkConfigFSM:
        return GrubMkConfigFSM()

    @pytest.fixture(scope="class")
    def grub_ab_slot(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        ab_slots: SlotMeta,
        _grub_mkconfig_fsm: GrubMkConfigFSM,
    ) -> tuple[Path, Path, Path]:
        """
        NOTE: this test simulating init and updating from a non-ota-partition enabled system
        NOTE: boot dirs for grub are located under boot folder
        boot folder structure:
            /boot
                ota-partition(symlink->ota-partition.sda2)
                ota-partition.sda2/
                ota-partition.sda3/
                ota/
        """
        slot_a = Path(ab_slots.slot_a)
        slot_b = Path(ab_slots.slot_b)
        boot_dir = tmp_path_factory.mktemp("boot")

        slot_b_boot_dir = slot_b / "boot"
        slot_b_boot_dir.mkdir(parents=True, exist_ok=True)

        # copy the contents from pre-populated boot_dir to test /boot folder
        # NOTE: check kernel version from the ota-test_base image Dockerfile
        shutil.copytree(
            Path(ab_slots.slot_a_boot_dev) / Path(cfg.BOOT_DIR).relative_to("/"),
            boot_dir,
            dirs_exist_ok=True,
        )

        # NOTE: dummy ota-image doesn't have grub installed,
        #       so we need to prepare /etc/default/grub by ourself
        default_grub = slot_a / Path(cfg.DEFAULT_GRUB_FILE).relative_to("/")
        default_grub.write_text((Path(__file__).parent / "default_grub").read_text())

        # prepare fstab file
        slot_a_fstab_file = slot_a / Path(cfg.FSTAB_FILE).relative_to("/")
        slot_a_fstab_file.write_text(self.FSTAB_ORIGIN)
        slot_b_fstab_file = slot_b / Path(cfg.FSTAB_FILE).relative_to("/")
        slot_b_fstab_file.parent.mkdir(parents=True, exist_ok=True)
        slot_b_fstab_file.write_text(self.FSTAB_ORIGIN)

        # prepare grub file for slot_a
        init_grub_file = boot_dir / Path(cfg.GRUB_FILE).relative_to("/boot")
        init_grub_file.parent.mkdir(parents=True, exist_ok=True)
        init_grub_file.write_text(_grub_mkconfig_fsm.GRUB_CFG_SLOT_A_NON_OTAPARTITION)

        return slot_a, slot_b, boot_dir

    @pytest.fixture
    def cfg_for_slot_a_as_current(
        self, mocker: pytest_mock.MockerFixture, grub_ab_slot: tuple[Path, Path, Path]
    ):
        slot_a, slot_b, boot_dir = grub_ab_slot
        _mock_otaclient_cfg = DefaultOTAClientConfigs()
        _mock_otaclient_cfg.STANDBY_SLOT_MNT = str(slot_b)  # type: ignore[assignment]
        _mock_otaclient_cfg.ACTIVE_SLOT_MNT = str(slot_a)  # type: ignore[assignment]
        _mock_otaclient_cfg.BOOT_DPATH = str(boot_dir)  # type: ignore[assignment]

        mocker.patch(f"{MODULE}.cfg", _mock_otaclient_cfg)

        _mock_grub_cfg = GrubControlConfig()
        _mock_grub_cfg.GRUB_DIR = str(boot_dir / "grub")  # type: ignore[assignment]
        _mock_grub_cfg.GRUB_CFG_PATH = str(boot_dir / "grub/grub.cfg")  # type: ignore[assignment]

        mocker.patch(f"{MODULE}.boot_cfg", _mock_grub_cfg)

    @pytest.fixture
    def cfg_for_slot_b_as_current(
        self, mocker: pytest_mock.MockerFixture, grub_ab_slot: tuple[Path, Path, Path]
    ):
        slot_a, slot_b, boot_dir = grub_ab_slot
        _mock_otaclient_cfg = DefaultOTAClientConfigs()
        _mock_otaclient_cfg.STANDBY_SLOT_MNT = str(slot_a)  # type: ignore[assignment]
        _mock_otaclient_cfg.ACTIVE_SLOT_MNT = str(slot_b)  # type: ignore[assignment]
        _mock_otaclient_cfg.BOOT_DPATH = str(boot_dir)  # type: ignore[assignment]

        mocker.patch(f"{MODULE}.cfg", _mock_otaclient_cfg)

        _mock_grub_cfg = GrubControlConfig()
        _mock_grub_cfg.GRUB_DIR = str(boot_dir / "grub")  # type: ignore[assignment]
        _mock_grub_cfg.GRUB_CFG_PATH = str(boot_dir / "grub/grub.cfg")  # type: ignore[assignment]

        mocker.patch(f"{MODULE}.boot_cfg", _mock_grub_cfg)

    @pytest.fixture(autouse=True)
    def mock_setup(
        self,
        mocker: pytest_mock.MockerFixture,
        grub_ab_slot: tuple[Path, Path, Path],
        _grub_mkconfig_fsm: GrubMkConfigFSM,
    ):
        from otaclient.boot_control._grub import GrubABPartitionDetector
        from otaclient.boot_control._slot_mnt_helper import SlotMountHelper

        slot_a, slot_b, _ = grub_ab_slot

        # ------ start fsm ------ #
        self._fsm = GrubFSM(slot_a_mp=slot_a, slot_b_mp=slot_b)

        # ------ mock SlotMountHelper ------ #
        _mocked_slot_mount_helper = mocker.MagicMock(spec=SlotMountHelper)
        type(_mocked_slot_mount_helper).standby_slot_dev = mocker.PropertyMock(
            wraps=self._fsm.get_standby_slot_dev
        )
        type(_mocked_slot_mount_helper).active_slot_dev = mocker.PropertyMock(
            wraps=self._fsm.get_active_slot_dev
        )
        type(_mocked_slot_mount_helper).standby_slot_mount_point = mocker.PropertyMock(
            wraps=self._fsm.get_standby_slot_mp
        )
        type(_mocked_slot_mount_helper).active_slot_mount_point = mocker.PropertyMock(
            wraps=self._fsm.get_active_slot_mp
        )
        type(_mocked_slot_mount_helper).standby_boot_dir = mocker.PropertyMock(
            wraps=self._fsm.get_standby_boot_dir
        )

        # ------ mock GrubABPartitionDetector ------ #
        _mocked_ab_partition_detector = mocker.MagicMock(spec=GrubABPartitionDetector)
        type(_mocked_ab_partition_detector).active_slot = mocker.PropertyMock(
            wraps=self._fsm.get_active_slot
        )
        type(_mocked_ab_partition_detector).active_dev = mocker.PropertyMock(
            wraps=self._fsm.get_active_slot_dev
        )
        type(_mocked_ab_partition_detector).standby_slot = mocker.PropertyMock(
            wraps=self._fsm.get_standby_slot
        )
        type(_mocked_ab_partition_detector).standby_dev = mocker.PropertyMock(
            wraps=self._fsm.get_standby_slot_dev
        )

        ###### mocking GrubHelper ######
        _grub_reboot_mock = mocker.MagicMock()
        mocker.patch(
            f"{cfg.GRUB_MODULE_PATH}.GrubHelper.grub_reboot", _grub_reboot_mock
        )
        # bind to test instance
        self._grub_reboot_mock = _grub_reboot_mock

        ###### mocking cmdhelper ######
        _cmdhelper_mock = mocker.MagicMock()
        _cmdhelper_mock.reboot.side_effect = self._fsm.switch_boot  # type: ignore
        _cmdhelper_mock.get_attrs_by_dev = mocker.MagicMock(
            wraps=self._fsm.get_attrs_by_dev
        )
        # bind the mocker to the test instance
        self._cmdhelper_mock = _cmdhelper_mock

        ###### mock GrubHelper ######
        _grub_mkconfig_path = f"{cfg.GRUB_MODULE_PATH}.GrubHelper.grub_mkconfig"
        mocker.patch(
            _grub_mkconfig_path,
            wraps=_grub_mkconfig_fsm.grub_mkconfig,
        )

        ###### patching ######
        # patch cmdhelper
        _cmdhelper_at_grub_path = f"{cfg.GRUB_MODULE_PATH}.cmdhelper"
        mocker.patch(_cmdhelper_at_grub_path, _cmdhelper_mock)
        # patch _GrubABPartitionDetector
        _grub_ab_partition_detector_path = (
            f"{cfg.GRUB_MODULE_PATH}.GrubABPartitionDetector"
        )
        mocker.patch(
            _grub_ab_partition_detector_path, return_value=_mocked_ab_partition_detector
        )
        # patch SlotMountHelper
        _slot_mount_helper_path = f"{cfg.GRUB_MODULE_PATH}.SlotMountHelper"
        mocker.patch(_slot_mount_helper_path, return_value=_mocked_slot_mount_helper)
        # patch reading from /proc/cmdline
        mocker.patch(
            f"{cfg.GRUB_MODULE_PATH}.cat_proc_cmdline",
            mocker.MagicMock(wraps=self._fsm.cat_proc_cmdline),
        )

    def test_grub_normal_update(
        self,
        cfg_for_slot_a_as_current,
        grub_ab_slot: tuple[Path, Path, Path],
    ):
        _, slot_b, boot_dir = grub_ab_slot
        slot_a_ota_partition_dir = Path(
            boot_dir / f"{cfg.OTA_PARTITION_DIRNAME}.{cfg.SLOT_A_ID_GRUB}"
        )
        slot_b_ota_partition_dir = Path(
            boot_dir / f"{cfg.OTA_PARTITION_DIRNAME}.{cfg.SLOT_B_ID_GRUB}"
        )

        ###### stage 1 ######
        # test init from non-ota-partition enabled system

        grub_controller = _grub.GrubController()
        assert (
            slot_a_ota_partition_dir / "status"
        ).read_text() == OTAStatus.INITIALIZED
        # assert ota-partition file points to slot_a ota-partition folder
        assert (
            os.readlink(boot_dir / cfg.OTA_PARTITION_DIRNAME)
            == f"{cfg.OTA_PARTITION_DIRNAME}.{cfg.SLOT_A_ID_GRUB}"
        )
        assert (
            boot_dir / "grub/grub.cfg"
        ).read_text() == GrubMkConfigFSM.GRUB_CFG_SLOT_A_UPDATED

        # test pre-update
        grub_controller.pre_update(
            version=cfg.UPDATE_VERSION,
            standby_as_ref=False,  # NOTE: not used
            erase_standby=False,  # NOTE: not used
        )
        # update slot_b, slot_a_ota_status->FAILURE, slot_b_ota_status->UPDATING
        assert (slot_a_ota_partition_dir / "status").read_text() == OTAStatus.FAILURE
        assert (slot_b_ota_partition_dir / "status").read_text() == OTAStatus.UPDATING
        # NOTE: we have to copy the new kernel files to the slot_b's boot dir
        #       this is done by the create_standby module
        _kernel = f"{cfg.KERNEL_PREFIX}-{cfg.KERNEL_VERSION}"
        _initrd = f"{cfg.INITRD_PREFIX}-{cfg.KERNEL_VERSION}"
        shutil.copy(slot_a_ota_partition_dir / _kernel, slot_b / "boot")
        shutil.copy(slot_a_ota_partition_dir / _initrd, slot_b / "boot")

        logger.info("pre-update completed, entering post-update...")

        # test post-update
        grub_controller: Any  # for typing
        grub_controller.post_update()
        grub_controller.finalizing_update()

        assert (
            slot_b / Path(cfg.FSTAB_FILE).relative_to("/")
        ).read_text().strip() == self.FSTAB_UPDATED.strip()
        assert (
            boot_dir / "grub/grub.cfg"
        ).read_text().strip() == GrubMkConfigFSM.GRUB_CFG_SLOT_A_UPDATED.strip()
        # NOTE: check grub.cfg_slot_a_post_update, the target entry is 0
        self._grub_reboot_mock.assert_called_once_with(0)
        self._cmdhelper_mock.reboot.assert_called_once()  # type: ignore

        ###### stage 2 ######
        # test init after first reboot

        # NOTE: dummy ota-image doesn't have grub installed,
        #       so we need to prepare /etc/default/grub by ourself
        default_grub = slot_b / Path(cfg.DEFAULT_GRUB_FILE).relative_to("/")
        default_grub.parent.mkdir(parents=True, exist_ok=True)
        default_grub.write_text(self.DEFAULT_GRUB)

        logger.info("post-update completed, test init after first reboot...")

        ### test pre-init ###
        assert self._fsm.is_boot_switched
        assert (slot_b_ota_partition_dir / "status").read_text() == OTAStatus.UPDATING
        # assert ota-partition file is not yet switched before first reboot init
        assert (
            os.readlink(boot_dir / cfg.OTA_PARTITION_DIRNAME)
            == f"{cfg.OTA_PARTITION_DIRNAME}.{cfg.SLOT_A_ID_GRUB}"
        )

        ### test first reboot init ###
        _ = _grub.GrubController()
        # assert ota-partition file switch to slot_b ota-partition folder after first reboot init
        assert (
            os.readlink(boot_dir / cfg.OTA_PARTITION_DIRNAME)
            == f"{cfg.OTA_PARTITION_DIRNAME}.{cfg.SLOT_B_ID_GRUB}"
        )
        assert (slot_b_ota_partition_dir / "status").read_text() == OTAStatus.SUCCESS
        assert (slot_b_ota_partition_dir / "version").read_text() == cfg.UPDATE_VERSION


@pytest.mark.parametrize(
    "_input, default_entry, expected",
    (
        (
            # test point:
            #   1. GRUB_TIMEOUT should be set as predefined default value, and only present once,
            #   2. GRUB_DISABLE_SUBMENU should be updated as predefined default value,
            #   3. already presented options that should be preserved should present,
            #   4. all predefined default options should be set.
            #   5. GRUB_TIMEOUT_STYLE which specified multiple times should be merged into one,
            #      and take the latest specified value,
            #   6. GRUB_DEFAULT is updated as <default_entry>,
            #   7. allow '=' sign within option value,
            #   8. empty lines and comments are removed.
            """\
GRUB_DEFAULT=6
X_DUPLICATED_OPTIONS=100
GRUB_TIMEOUT_STYLE=hidden
GRUB_TIMEOUT=99

# some comments here
GRUB_DISTRIBUTOR=`lsb_release -i -s 2> /dev/null || echo Debian`
GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"
X_DUPLICATED_OPTIONS=200
GRUB_CMDLINE_LINUX=""
GRUB_DISABLE_SUBMENU=n
GRUB_TIMEOUT=60
GRUB_TIMEOUT=30
X_DUPLICATED_OPTIONS=1
X_OPTION_WITH_EQUAL_SIGN=a=b=c=d
""",
            999,
            """\
# This file is generated by otaclient, modification might not be preserved across OTA.
GRUB_DEFAULT=999
X_DUPLICATED_OPTIONS=1
GRUB_TIMEOUT_STYLE=menu
GRUB_TIMEOUT=0
GRUB_DISTRIBUTOR=`lsb_release -i -s 2> /dev/null || echo Debian`
GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"
GRUB_CMDLINE_LINUX=""
GRUB_DISABLE_SUBMENU=y
X_OPTION_WITH_EQUAL_SIGN=a=b=c=d
GRUB_DISABLE_OS_PROBER=true
GRUB_DISABLE_RECOVERY=true
""",
        ),
    ),
)
def test_update_grub_default(
    _input: str, default_entry: typing.Optional[int], expected: str
):
    from otaclient.boot_control._grub import GrubHelper

    updated = GrubHelper.update_grub_default(_input, default_entry_idx=default_entry)
    assert updated == expected
