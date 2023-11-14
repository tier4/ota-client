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
import pytest
import pytest_mock
from pathlib import Path

from otaclient._utils.path import replace_root
from otaclient.app.boot_control.configs import GrubControlConfig
from otaclient.app.configs import Config as otaclient_Config
from otaclient.app.proto import wrapper

from tests.utils import SlotMeta
from tests.conftest import TestConfiguration as test_cfg

logger = logging.getLogger(__name__)


class GrubFSM:
    def __init__(self, slot_a_mp, slot_b_mp) -> None:
        self._current_slot = test_cfg.SLOT_A_ID_GRUB
        self._standby_slot = test_cfg.SLOT_B_ID_GRUB
        self._current_slot_mp = Path(slot_a_mp)
        self._standby_slot_mp = Path(slot_b_mp)
        self._current_slot_dev_uuid = f"UUID={test_cfg.SLOT_A_UUID}"
        self._standby_slot_dev_uuid = f"UUID={test_cfg.SLOT_B_UUID}"
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

    def get_uuid_str_by_dev(self, dev: str):
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
        if self._current_slot == test_cfg.SLOT_A_ID_GRUB:
            return test_cfg.CMDLINE_SLOT_A
        else:
            return test_cfg.CMDLINE_SLOT_B


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

    @pytest.fixture
    def grub_ab_slot(self, ab_slots: SlotMeta):
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

        #
        # ------ init otaclient configs ------ #
        #
        self.mocked_otaclient_cfg_slot_a = otaclient_Config(
            ACTIVE_ROOTFS=ab_slots.slot_a
        )
        self.mocked_otaclient_cfg_slot_b = otaclient_Config(
            ACTIVE_ROOTFS=ab_slots.slot_b
        )

        #
        # ----- setup slots ------ #
        #
        self.slot_a = Path(ab_slots.slot_a)
        self.slot_b = Path(ab_slots.slot_b)
        # NOTE: grub uses share boot device, here we use slot_a_boot as shared boot
        self.shared_boot_dir = Path(ab_slots.slot_a_boot_dev)

        self.slot_a_ota_partition_dir = (
            self.shared_boot_dir
            / f"{test_cfg.OTA_PARTITION_DIRNAME}.{test_cfg.SLOT_A_ID_GRUB}"
        )
        self.slot_b_ota_partition_dir = (
            self.shared_boot_dir
            / f"{test_cfg.OTA_PARTITION_DIRNAME}.{test_cfg.SLOT_B_ID_GRUB}"
        )

        # copy the contents from slot_a's pre-populated boot_dir to test /boot folder
        # NOTE: check kernel version from the ota-test_base image Dockerfile
        shutil.copytree(
            self.slot_a / "boot",
            self.shared_boot_dir,
            dirs_exist_ok=True,
        )
        shutil.rmtree(self.slot_a / "boot", ignore_errors=True)
        # simulate boot device mounted on slot_a
        (self.slot_a / "boot").symlink_to(self.shared_boot_dir)

        # create slot_b's boot folder
        (self.slot_b / "boot").mkdir(exist_ok=True, parents=True)

    @pytest.fixture(autouse=True)
    def mock_setup(
        self,
        mocker: pytest_mock.MockerFixture,
        grub_ab_slot,
    ):
        from otaclient.app.boot_control._grub import GrubABPartitionDetector
        from otaclient.app.boot_control._common import CMDHelperFuncs, SlotMountHelper

        # ------ start fsm ------ #
        self._fsm = GrubFSM(slot_a_mp=self.slot_a, slot_b_mp=self.slot_b)

        #
        # ------ apply otaclient cfg patch ------ #
        #
        mocker.patch(
            f"{test_cfg.BOOT_CONTROL_CONFIG_MODULE_PATH}.cfg",
            self.mocked_otaclient_cfg_slot_a,
        )
        mocker.patch(
            f"{test_cfg.GRUB_MODULE_PATH}.cfg", self.mocked_otaclient_cfg_slot_a
        )
        # NOTE: remember to also patch otaclient cfg in boot.common module
        mocker.patch(
            f"{test_cfg.BOOT_CONTROL_COMMON_MODULE_PATH}.cfg",
            self.mocked_otaclient_cfg_slot_a,
        )

        # after the mocked otaclient cfg is applied, we can init rpi_boot cfg instance
        self.mocked_boot_cfg_slot_a = GrubControlConfig()

        #
        # ------ mock SlotMountHelper ------ #
        #
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

        mocker.patch(
            f"{test_cfg.GRUB_MODULE_PATH}.SlotMountHelper",
            return_value=_mocked_slot_mount_helper,
        )

        #
        # ------ patching GrubABPartitionDetector ------ #
        #
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

        mocker.patch(
            f"{test_cfg.GRUB_MODULE_PATH}.GrubABPartitionDetector",
            return_value=_mocked_ab_partition_detector,
        )

        #
        # ------ patching GrubHelper ------ #
        #
        _grub_reboot_mock = mocker.MagicMock()
        mocker.patch(
            f"{test_cfg.GRUB_MODULE_PATH}.GrubHelper.grub_reboot", _grub_reboot_mock
        )
        # bind to test instance
        self._grub_reboot_mock = _grub_reboot_mock

        #
        # ------ patching CMDHelperFuncs ------ #
        #
        _cmdhelper_mock = typing.cast(
            CMDHelperFuncs, mocker.MagicMock(spec=CMDHelperFuncs)
        )

        _cmdhelper_mock.reboot.side_effect = self._fsm.switch_boot
        _cmdhelper_mock.get_uuid_str_by_dev = mocker.MagicMock(
            wraps=self._fsm.get_uuid_str_by_dev
        )
        # bind the mocker to the test instance
        self._cmdhelper_mock = _cmdhelper_mock

        # NOTE: also remember to patch CMDHelperFuncs in boot.common
        mocker.patch(
            f"{test_cfg.BOOT_CONTROL_COMMON_MODULE_PATH}.CMDHelperFuncs",
            _cmdhelper_mock,
        )
        mocker.patch(f"{test_cfg.GRUB_MODULE_PATH}.CMDHelperFuncs", _cmdhelper_mock)

        #
        # ------ patching GrubHelper ------ #
        #
        _grub_mkconfig_path = f"{test_cfg.GRUB_MODULE_PATH}.GrubHelper.grub_mkconfig"
        mocker.patch(
            _grub_mkconfig_path,
            wraps=self._grub_mkconfig_fsm.grub_mkconfig,
        )

        # patch reading from /proc/cmdline
        mocker.patch(
            f"{test_cfg.GRUB_MODULE_PATH}.cat_proc_cmdline",
            mocker.MagicMock(wraps=self._fsm.cat_proc_cmdline),
        )

    @pytest.fixture(autouse=True)
    def setup_test(self, mock_setup):
        # NOTE: dummy ota-image doesn't have grub installed,
        #       so we need to prepare /etc/default/grub by ourself
        default_grub = Path(self.mocked_boot_cfg_slot_a.GRUB_DEFAULT_FPATH)
        default_grub.write_text((Path(__file__).parent / "default_grub").read_text())

        # prepare fstab file
        slot_a_fstab_file = Path(self.mocked_otaclient_cfg_slot_a.FSTAB_FPATH)
        slot_a_fstab_file.write_text(self.FSTAB_ORIGIN)

        slot_b_fstab_file = Path(
            replace_root(slot_a_fstab_file, self.slot_a, self.slot_b)
        )
        slot_b_fstab_file.parent.mkdir(parents=True, exist_ok=True)
        slot_b_fstab_file.write_text(self.FSTAB_ORIGIN)

        # prepare grub file for slot_a
        init_grub_file = Path(self.mocked_boot_cfg_slot_a.GRUB_CFG_FPATH)
        init_grub_file.parent.mkdir(parents=True, exist_ok=True)
        init_grub_file.write_text(
            self._grub_mkconfig_fsm.GRUB_CFG_SLOT_A_NON_OTAPARTITION
        )

        #
        # ------ setup mount space ------ #
        #
        # NOTE: as we mock CMDHelpers, mount is not executed, so we prepare the mount points
        #   by ourselves.(In the future we can use FSM to do it.)
        Path(self.mocked_otaclient_cfg_slot_a.OTACLIENT_MOUNT_SPACE_DPATH).mkdir(
            parents=True, exist_ok=True
        )
        Path(self.mocked_otaclient_cfg_slot_a.ACTIVE_SLOT_MP).symlink_to(self.slot_a)
        Path(self.mocked_otaclient_cfg_slot_a.STANDBY_SLOT_MP).symlink_to(self.slot_b)

    def test_grub_normal_update(self, mocker: pytest_mock.MockerFixture):
        from otaclient.app.boot_control.configs import GrubControlConfig
        from otaclient.app.boot_control._grub import GrubController

        #
        # ------ stage 1 ------ #
        #
        # test init from non-ota-partition enabled system

        grub_controller = GrubController()
        assert (
            self.slot_a_ota_partition_dir / otaclient_Config.OTA_STATUS_FNAME
        ).read_text() == wrapper.StatusOta.INITIALIZED.name
        # assert ota-partition file points to slot_a ota-partition folder
        assert (
            os.readlink(self.shared_boot_dir / test_cfg.OTA_PARTITION_DIRNAME)
            == f"{test_cfg.OTA_PARTITION_DIRNAME}.{test_cfg.SLOT_A_ID_GRUB}"
        )
        assert (
            Path(self.mocked_boot_cfg_slot_a.GRUB_CFG_FPATH).read_text()
            == GrubMkConfigFSM.GRUB_CFG_SLOT_A_UPDATED
        )

        # test pre-update
        logger.info("[TESTING] execute pre_update ...")
        grub_controller.pre_update(
            version=test_cfg.UPDATE_VERSION,
            standby_as_ref=False,  # NOTE: not used
            erase_standby=False,  # NOTE: not used
        )

        # update slot_b, slot_a_ota_status->FAILURE, slot_b_ota_status->UPDATING
        assert (
            self.slot_a_ota_partition_dir / otaclient_Config.OTA_STATUS_FNAME
        ).read_text() == wrapper.StatusOta.FAILURE.name
        assert (
            self.slot_b_ota_partition_dir / otaclient_Config.OTA_STATUS_FNAME
        ).read_text() == wrapper.StatusOta.UPDATING.name

        # NOTE: we have to copy the new kernel files to the slot_b's boot dir
        #       this is done by the create_standby module
        _kernel = f"{test_cfg.KERNEL_PREFIX}-{test_cfg.KERNEL_VERSION}"
        _initrd = f"{test_cfg.INITRD_PREFIX}-{test_cfg.KERNEL_VERSION}"
        shutil.copy(self.slot_a_ota_partition_dir / _kernel, self.slot_b / "boot")
        shutil.copy(self.slot_a_ota_partition_dir / _initrd, self.slot_b / "boot")

        logger.info("[TESTING] pre-update completed, entering post-update...")
        # test post-update
        _post_updater = grub_controller.post_update()
        next(_post_updater)
        next(_post_updater, None)

        # ensure the standby slot's fstab file is updated with slot_b's UUID
        assert (
            Path(self.mocked_otaclient_cfg_slot_b.FSTAB_FPATH).read_text().strip()
            == self.FSTAB_UPDATED.strip()
        )
        # ensure /boot/grub/grub.cfg is updated
        assert (
            Path(self.mocked_boot_cfg_slot_a.GRUB_CFG_FPATH).read_text().strip()
            == GrubMkConfigFSM.GRUB_CFG_SLOT_A_UPDATED.strip()
        )
        # NOTE: check grub.cfg_slot_a_post_update, the target entry is 0
        self._grub_reboot_mock.assert_called_once_with(0)
        self._cmdhelper_mock.reboot.assert_called_once()

        #
        # ------ stage 2 ------ #
        #
        # active slot: slot_b
        # test init after first reboot

        # simulate boot dev mounted on slot_b
        shutil.rmtree(self.slot_b / "boot", ignore_errors=True)
        (self.slot_b / "boot").symlink_to(self.shared_boot_dir)

        # NOTE: dummy ota-image doesn't have grub installed,
        #       so we need to prepare /etc/default/grub by ourself
        default_grub = self.slot_b / Path(test_cfg.DEFAULT_GRUB_FILE).relative_to("/")
        default_grub.parent.mkdir(parents=True, exist_ok=True)
        default_grub.write_text(self.DEFAULT_GRUB)

        logger.info("[TESTING] post-update completed, test init after first reboot...")

        # patch otaclient cfg for slot_b
        mocker.patch(
            f"{test_cfg.BOOT_CONTROL_CONFIG_MODULE_PATH}.cfg",
            self.mocked_otaclient_cfg_slot_b,
        )
        mocker.patch(
            f"{test_cfg.GRUB_MODULE_PATH}.cfg", self.mocked_otaclient_cfg_slot_b
        )
        # NOTE: remember to also patch otaclient cfg in boot.common module
        mocker.patch(
            f"{test_cfg.BOOT_CONTROL_COMMON_MODULE_PATH}.cfg",
            self.mocked_otaclient_cfg_slot_b,
        )

        # NOTE: old grub boot_cfg's properties are cached, so create a new one
        _recreated_grub_ctrl_cfg = GrubControlConfig()
        mocker.patch(f"{test_cfg.GRUB_MODULE_PATH}.boot_cfg", _recreated_grub_ctrl_cfg)

        ### test pre-init ###
        assert self._fsm.is_boot_switched
        assert (
            self.slot_b_ota_partition_dir / otaclient_Config.OTA_STATUS_FNAME
        ).read_text() == wrapper.StatusOta.UPDATING.name
        # assert ota-partition file is not yet switched before first reboot init
        assert (
            os.readlink(self.shared_boot_dir / test_cfg.OTA_PARTITION_DIRNAME)
            == f"{test_cfg.OTA_PARTITION_DIRNAME}.{test_cfg.SLOT_A_ID_GRUB}"
        )

        ### test first reboot init ###
        _ = GrubController()
        # assert ota-partition file switch to slot_b ota-partition folder after first reboot init
        assert (
            os.readlink(self.shared_boot_dir / test_cfg.OTA_PARTITION_DIRNAME)
            == f"{test_cfg.OTA_PARTITION_DIRNAME}.{test_cfg.SLOT_B_ID_GRUB}"
        )
        assert (
            self.slot_b_ota_partition_dir / otaclient_Config.OTA_STATUS_FNAME
        ).read_text() == wrapper.StatusOta.SUCCESS.name
        assert (
            self.slot_b_ota_partition_dir / otaclient_Config.OTA_VERSION_FNAME
        ).read_text() == test_cfg.UPDATE_VERSION


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
