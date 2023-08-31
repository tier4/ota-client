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


import os
import shutil
import typing
import pytest
import pytest_mock
from pathlib import Path

import logging

from tests.utils import SlotMeta
from tests.conftest import TestConfiguration as cfg

logger = logging.getLogger(__name__)


class GrubFSM:
    def __init__(self) -> None:
        self.current_slot = cfg.SLOT_A_ID_GRUB
        self.standby_slot = cfg.SLOT_B_ID_GRUB
        self.current_slot_bootable = True
        self.standby_slot_bootable = True

        self.is_boot_switched = False

    def get_current_rootfs_dev(self):
        return f"/dev/{self.current_slot}"

    def get_standby_rootfs_dev(self):
        return f"/dev/{self.standby_slot}"

    def get_current_slot(self):
        return self.current_slot

    def get_standby_slot(self):
        return self.standby_slot

    def get_uuid_str_by_dev(self, dev: str):
        if dev == self.get_standby_rootfs_dev():
            return f"UUID={cfg.SLOT_B_UUID}"
        else:
            return f"UUID={cfg.SLOT_A_UUID}"

    def switch_boot(self):
        self.current_slot, self.standby_slot = self.standby_slot, self.current_slot
        self.is_boot_switched = True

    def cat_proc_cmdline(self):
        if self.current_slot == cfg.SLOT_A_ID_GRUB:
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

    def cfg_for_slot_a_as_current(self):
        from otaclient.app.boot_control.configs import GrubControlConfig

        _mocked_grub_cfg = GrubControlConfig()
        _mocked_grub_cfg.MOUNT_POINT = str(self.slot_b)  # type: ignore
        _mocked_grub_cfg.ACTIVE_ROOTFS_PATH = str(self.slot_a)  # type: ignore
        _mocked_grub_cfg.BOOT_DIR = str(  # type: ignore
            self.boot_dir
        )  # unified boot dir
        _mocked_grub_cfg.GRUB_DIR = str(self.boot_dir / "grub")
        _mocked_grub_cfg.GRUB_CFG_PATH = str(self.boot_dir / "grub/grub.cfg")

        return _mocked_grub_cfg

    def cfg_for_slot_b_as_current(self):
        from otaclient.app.boot_control.configs import GrubControlConfig

        _mocked_grub_cfg = GrubControlConfig()
        _mocked_grub_cfg.MOUNT_POINT = str(self.slot_a)  # type: ignore
        _mocked_grub_cfg.ACTIVE_ROOTFS_PATH = str(self.slot_b)  # type: ignore
        _mocked_grub_cfg.BOOT_DIR = str(  # type: ignore
            self.boot_dir
        )  # unified boot dir
        _mocked_grub_cfg.GRUB_DIR = str(self.boot_dir / "grub")
        _mocked_grub_cfg.GRUB_CFG_PATH = str(self.boot_dir / "grub/grub.cfg")

        return _mocked_grub_cfg

    @pytest.fixture
    def grub_ab_slot(self, tmp_path: Path, ab_slots: SlotMeta):
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
        self._grub_mkconfig_fsm = GrubMkConfigFSM()

        self.slot_a = Path(ab_slots.slot_a)
        self.slot_b = Path(ab_slots.slot_b)
        self.boot_dir = tmp_path / Path(cfg.BOOT_DIR).relative_to("/")
        self.slot_a_ota_partition_dir = (
            self.boot_dir / f"{cfg.OTA_PARTITION_DIRNAME}.{cfg.SLOT_A_ID_GRUB}"
        )
        self.slot_b_ota_partition_dir = (
            self.boot_dir / f"{cfg.OTA_PARTITION_DIRNAME}.{cfg.SLOT_B_ID_GRUB}"
        )
        # copy the contents from pre-populated boot_dir to test /boot folder
        # NOTE: check kernel version from the ota-test_base image Dockerfile
        shutil.copytree(
            Path(ab_slots.slot_a_boot_dev) / Path(cfg.BOOT_DIR).relative_to("/"),
            self.boot_dir,
            dirs_exist_ok=True,
        )
        # NOTE: dummy ota-image doesn't have grub installed,
        #       so we need to prepare /etc/default/grub by ourself
        default_grub = self.slot_a / Path(cfg.DEFAULT_GRUB_FILE).relative_to("/")
        default_grub.write_text((Path(__file__).parent / "default_grub").read_text())

        # prepare fstab file
        slot_a_fstab_file = self.slot_a / Path(cfg.FSTAB_FILE).relative_to("/")
        slot_a_fstab_file.write_text(self.FSTAB_ORIGIN)
        slot_b_fstab_file = self.slot_b / Path(cfg.FSTAB_FILE).relative_to("/")
        slot_b_fstab_file.parent.mkdir(parents=True, exist_ok=True)
        slot_b_fstab_file.write_text(self.FSTAB_ORIGIN)

        # prepare grub file for slot_a
        init_grub_file = self.boot_dir / Path(cfg.GRUB_FILE).relative_to("/boot")
        init_grub_file.parent.mkdir(parents=True, exist_ok=True)
        init_grub_file.write_text(
            self._grub_mkconfig_fsm.GRUB_CFG_SLOT_A_NON_OTAPARTITION
        )

    @pytest.fixture(autouse=True)
    def mock_setup(
        self,
        mocker: pytest_mock.MockerFixture,
        grub_ab_slot,
    ):
        from otaclient.app.boot_control._grub import GrubABPartitionDetecter
        from otaclient.app.boot_control._common import CMDHelperFuncs

        ###### start fsm ######
        self._fsm = GrubFSM()

        ###### mocking GrubABPartitionDetecter ######
        _GrubABPartitionDetecter_mock = typing.cast(
            GrubABPartitionDetecter, mocker.MagicMock(spec=GrubABPartitionDetecter)
        )
        _GrubABPartitionDetecter_mock.get_standby_slot = mocker.MagicMock(
            wraps=self._fsm.get_standby_slot
        )
        _GrubABPartitionDetecter_mock.get_active_slot = mocker.MagicMock(
            wraps=self._fsm.get_current_slot
        )
        _GrubABPartitionDetecter_mock.get_active_slot_dev = mocker.MagicMock(
            wraps=self._fsm.get_current_rootfs_dev
        )
        _GrubABPartitionDetecter_mock.get_standby_slot_dev = mocker.MagicMock(
            wraps=self._fsm.get_standby_rootfs_dev
        )

        ###### mocking GrubHelper ######
        _grub_reboot_mock = mocker.MagicMock()
        mocker.patch(
            f"{cfg.GRUB_MODULE_PATH}.GrubHelper.grub_reboot", _grub_reboot_mock
        )
        # bind to test instance
        self._grub_reboot_mock = _grub_reboot_mock

        ###### mocking CMDHelperFuncs ######
        _CMDHelper_mock = typing.cast(
            CMDHelperFuncs, mocker.MagicMock(spec=CMDHelperFuncs)
        )
        _CMDHelper_mock.reboot.side_effect = self._fsm.switch_boot
        _CMDHelper_mock.get_uuid_str_by_dev = mocker.MagicMock(
            wraps=self._fsm.get_uuid_str_by_dev
        )
        # bind the mocker to the test instance
        self._CMDHelper_mock = _CMDHelper_mock

        ###### mock GrubHelper ######
        _grub_mkconfig_path = f"{cfg.GRUB_MODULE_PATH}.GrubHelper.grub_mkconfig"
        mocker.patch(
            _grub_mkconfig_path,
            wraps=self._grub_mkconfig_fsm.grub_mkconfig,
        )

        ###### patching ######
        # patch CMDHelper
        # NOTE: also remember to patch CMDHelperFuncs in common
        _CMDHelper_at_common_path = (
            f"{cfg.BOOT_CONTROL_COMMON_MODULE_PATH}.CMDHelperFuncs"
        )
        _CMDHelper_at_grub_path = f"{cfg.GRUB_MODULE_PATH}.CMDHelperFuncs"
        mocker.patch(_CMDHelper_at_common_path, _CMDHelper_mock)
        mocker.patch(_CMDHelper_at_grub_path, _CMDHelper_mock)
        # patch _GrubABPartitionDetecter
        _GrubABPartitionDetecter_path = (
            f"{cfg.GRUB_MODULE_PATH}.GrubABPartitionDetecter"
        )
        mocker.patch(
            _GrubABPartitionDetecter_path, return_value=_GrubABPartitionDetecter_mock
        )
        # patch reading from /proc/cmdline
        mocker.patch(
            f"{cfg.GRUB_MODULE_PATH}.cat_proc_cmdline",
            mocker.MagicMock(wraps=self._fsm.cat_proc_cmdline),
        )

    def test_grub_normal_update(self, mocker: pytest_mock.MockerFixture):
        from otaclient.app.boot_control._grub import GrubController

        _cfg_patch_path = f"{cfg.GRUB_MODULE_PATH}.cfg"

        ###### stage 1 ######
        # test init from non-ota-partition enabled system

        # mock cfg
        mocker.patch(_cfg_patch_path, self.cfg_for_slot_a_as_current())

        grub_controller = GrubController()
        assert (self.slot_a_ota_partition_dir / "status").read_text() == "INITIALIZED"
        # assert ota-partition file points to slot_a ota-partition folder
        assert (
            os.readlink(self.boot_dir / cfg.OTA_PARTITION_DIRNAME)
            == f"{cfg.OTA_PARTITION_DIRNAME}.{cfg.SLOT_A_ID_GRUB}"
        )
        assert (
            self.boot_dir / "grub/grub.cfg"
        ).read_text() == GrubMkConfigFSM.GRUB_CFG_SLOT_A_UPDATED

        # test pre-update
        grub_controller.pre_update(
            version=cfg.UPDATE_VERSION,
            standby_as_ref=False,  # NOTE: not used
            erase_standby=False,  # NOTE: not used
        )
        # update slot_b, slot_a_ota_status->FAILURE, slot_b_ota_status->UPDATING
        assert (self.slot_a_ota_partition_dir / "status").read_text() == "FAILURE"
        assert (self.slot_b_ota_partition_dir / "status").read_text() == "UPDATING"
        # NOTE: we have to copy the new kernel files to the slot_b's boot dir
        #       this is done by the create_standby module
        _kernel = f"{cfg.KERNEL_PREFIX}-{cfg.KERNEL_VERSION}"
        _initrd = f"{cfg.INITRD_PREFIX}-{cfg.KERNEL_VERSION}"
        shutil.copy(
            self.slot_a_ota_partition_dir / _kernel, self.slot_b_ota_partition_dir
        )
        shutil.copy(
            self.slot_a_ota_partition_dir / _initrd, self.slot_b_ota_partition_dir
        )

        logger.info("pre-update completed, entering post-update...")
        # test post-update
        _post_updater = grub_controller.post_update()
        next(_post_updater)
        next(_post_updater, None)
        assert (
            self.slot_b / Path(cfg.FSTAB_FILE).relative_to("/")
        ).read_text() == self.FSTAB_UPDATED.strip()
        assert (
            self.boot_dir / "grub/grub.cfg"
        ).read_text() == GrubMkConfigFSM.GRUB_CFG_SLOT_A_UPDATED
        # NOTE: check grub.cfg_slot_a_post_update, the target entry is 0
        self._grub_reboot_mock.assert_called_once_with(0)
        self._CMDHelper_mock.reboot.assert_called_once()

        ###### stage 2 ######
        # test init after first reboot

        # NOTE: dummy ota-image doesn't have grub installed,
        #       so we need to prepare /etc/default/grub by ourself
        default_grub = self.slot_b / Path(cfg.DEFAULT_GRUB_FILE).relative_to("/")
        default_grub.parent.mkdir(parents=True, exist_ok=True)
        default_grub.write_text(self.DEFAULT_GRUB)

        logger.info("post-update completed, test init after first reboot...")
        # mock cfg
        mocker.patch(_cfg_patch_path, self.cfg_for_slot_b_as_current())

        ### test pre-init ###
        assert self._fsm.is_boot_switched
        assert (self.slot_b_ota_partition_dir / "status").read_text() == "UPDATING"
        # assert ota-partition file is not yet switched before first reboot init
        assert (
            os.readlink(self.boot_dir / cfg.OTA_PARTITION_DIRNAME)
            == f"{cfg.OTA_PARTITION_DIRNAME}.{cfg.SLOT_A_ID_GRUB}"
        )

        ### test first reboot init ###
        grub_controller = GrubController()
        # assert ota-partition file switch to slot_b ota-partition folder after first reboot init
        assert (
            os.readlink(self.boot_dir / cfg.OTA_PARTITION_DIRNAME)
            == f"{cfg.OTA_PARTITION_DIRNAME}.{cfg.SLOT_B_ID_GRUB}"
        )
        assert (self.slot_b_ota_partition_dir / "status").read_text() == "SUCCESS"
        assert (
            self.slot_b_ota_partition_dir / "version"
        ).read_text() == cfg.UPDATE_VERSION


@pytest.mark.parametrize(
    "input, default_entry, expected",
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
    input: str, default_entry: typing.Optional[int], expected: str
):
    from otaclient.app.boot_control._grub import GrubHelper

    updated = GrubHelper.update_grub_default(input, default_entry_idx=default_entry)
    assert updated == expected
