from functools import wraps
import shutil
import typing
import pytest
import pytest_mock
from pathlib import Path

import logging

from tests.utils import SlotMeta

logger = logging.getLogger(__name__)

# NOTE: check ota-test_base Dockerfile
KERNEL_VERSION = "5.8.0-53-generic"


class GrubFSM:
    SLOT_A_UUID = "aaaaaaaa-0000-0000-0000-aaaaaaaaaaaa"
    SLOT_B_UUID = "bbbbbbbb-1111-1111-1111-bbbbbbbbbbbb"
    CMDLINE_SLOT_A = r"BOOT_IMAGE=/vmlinuz-5.8.0-53-generic root=UUID=aaaaaaaa-0000-0000-0000-aaaaaaaaaaaa ro quiet splash"
    CMDLINE_SLOT_B = r"BOOT_IMAGE=/vmlinuz-ota.standby root=UUID=bbbbbbbb-1111-1111-1111-bbbbbbbbbbbb ro quiet splash"
    SLOT_A = "sda2"  # startup slot
    SLOT_B = "sda3"

    def __init__(self) -> None:
        self.current_slot = self.SLOT_A
        self.standby_slot = self.SLOT_B
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
            return f"UUID={self.SLOT_B_UUID}"
        else:
            return f"UUID={self.SLOT_A_UUID}"

    def switch_boot(self):
        self.current_slot, self.standby_slot = self.standby_slot, self.current_slot
        self.is_boot_switched = True

    def cat_proc_cmdline(self):
        if self.current_slot == self.SLOT_A:
            return self.CMDLINE_SLOT_A
        else:
            return self.CMDLINE_SLOT_B


class GrubMkConfigFSM:
    GRUB_CFG_SLOT_A_NON_OTAPARTITION = (
        Path(__file__).parent / "grub.cfg_slot_a_non_otapartition"
    ).read_text()
    GRUB_CFG_SLOT_A_OTAPARTITION = (
        Path(__file__).parent / "grub.cfg_slot_a"
    ).read_text()
    GRUB_CFG_SLOT_A_POST_UPDATE = (
        Path(__file__).parent / "grub.cfg_slot_a_post_update"
    ).read_text()
    GRUB_CFG_SLOT_B = (Path(__file__).parent / "grub.cfg_slot_b").read_text()
    GRUB_CFG_SLOT_B_POST_INIT = (
        Path(__file__).parent / "grub.cfg_slot_b_post_init"
    ).read_text()

    _MAPPING = {
        0: GRUB_CFG_SLOT_A_OTAPARTITION,  # slot_a init1
        1: GRUB_CFG_SLOT_A_OTAPARTITION,  # slot_a init2
        #
        2: GRUB_CFG_SLOT_A_OTAPARTITION,  # slot_a post_update1
        3: GRUB_CFG_SLOT_A_OTAPARTITION,  # slot_a post_update2
        #
        4: GRUB_CFG_SLOT_B,  # slot_b pre_init
        5: GRUB_CFG_SLOT_B,
    }

    def __init__(self) -> None:
        self._count = 0

    def grub_mkconfig(self):
        logger.info(f"grub_mkconfig called: #{self._count}")
        res = self._MAPPING[self._count]
        self._count += 1
        return res


class TestGrubControl:
    CURRENT_VERSION = "123.x"
    UPDATE_VERSION = "789.x"
    FSTAB_ORIGIN = (Path(__file__).parent / "fstab_origin").read_text()
    DEFAULT_GRUB = (Path(__file__).parent / "default_grub").read_text()

    def cfg_for_slot_a_as_current(self):
        from app.configs import GrubControlConfig

        _mocked_grub_cfg = GrubControlConfig()
        _mocked_grub_cfg.MOUNT_POINT = str(self.slot_b)
        _mocked_grub_cfg.ACTIVE_ROOTFS_PATH = str(self.slot_a)
        _mocked_grub_cfg.BOOT_DIR = str(self.boot_dir)  # unified boot dir
        _mocked_grub_cfg.GRUB_DIR = str(self.boot_dir / "grub")
        _mocked_grub_cfg.GRUB_CFG_PATH = str(self.boot_dir / "grub/grub.cfg")

        return _mocked_grub_cfg

    def cfg_for_slot_b_as_current(self):
        from app.configs import GrubControlConfig

        _mocked_grub_cfg = GrubControlConfig()
        _mocked_grub_cfg.MOUNT_POINT = str(self.slot_a)
        _mocked_grub_cfg.ACTIVE_ROOTFS_PATH = str(self.slot_b)
        _mocked_grub_cfg.BOOT_DIR = str(self.boot_dir)  # unified boot dir
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
        self.boot_dir = tmp_path / "boot"
        self.slot_a_ota_partition_dir = self.boot_dir / "ota-partition.sda2"
        self.slot_b_ota_partition_dir = self.boot_dir / "ota-partition.sda3"
        # copy the contents from pre-populated boot_dir to test /boot folder
        # NOTE: check kernel version from the ota-test_base image Dockerfile
        shutil.copytree(
            Path(ab_slots.slot_a_boot_dev) / "boot",
            self.boot_dir,
            dirs_exist_ok=True,
        )
        # NOTE: dummy ota-image doesn't have grub installed,
        #       so we need to prepare /etc/default/grub by ourself
        default_grub = self.slot_a / "etc/default/grub"
        default_grub.write_text((Path(__file__).parent / "default_grub").read_text())

        # prepare fstab file
        slot_a_fstab_file = self.slot_a / "etc/fstab"
        slot_a_fstab_file.write_text(self.FSTAB_ORIGIN)
        slot_b_etc_dir = self.slot_b / "etc"
        slot_b_etc_dir.mkdir()
        fstab_file = slot_b_etc_dir / "fstab"
        fstab_file.write_text(self.FSTAB_ORIGIN)

        # prepare grub file for slot_a
        grub_dir = self.boot_dir / "grub"
        grub_dir.mkdir()
        grub_file = grub_dir / "grub.cfg"
        grub_file.write_text(self._grub_mkconfig_fsm.GRUB_CFG_SLOT_A_NON_OTAPARTITION)

    @pytest.fixture(autouse=True)
    def patch_current_used_boot_controller(self, mocker: pytest_mock.MockerFixture):
        mocker.patch("app.configs.BOOT_LOADER", "grub")

    @pytest.fixture(autouse=True)
    def mock_setup(
        self,
        mocker: pytest_mock.MockerFixture,
        patch_current_used_boot_controller,
        grub_ab_slot,
    ):
        from app.boot_control.grub import GrubABPartitionDetecter
        from app.boot_control.common import CMDHelperFuncs

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
        mocker.patch("app.boot_control.grub.GrubHelper.grub_reboot", _grub_reboot_mock)
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
        _grub_mkconfig_path = "app.boot_control.grub.GrubHelper.grub_mkconfig"
        mocker.patch(
            _grub_mkconfig_path,
            return_value=self._grub_mkconfig_fsm.grub_mkconfig(),
        )

        ###### patching ######
        # patch CMDHelper
        # NOTE: also remember to patch CMDHelperFuncs in common
        _CMDHelper_at_common_path = "app.boot_control.common.CMDHelperFuncs"
        _CMDHelper_at_grub_path = "app.boot_control.grub.CMDHelperFuncs"
        mocker.patch(_CMDHelper_at_common_path, _CMDHelper_mock)
        mocker.patch(_CMDHelper_at_grub_path, _CMDHelper_mock)
        # patch _GrubABPartitionDetecter
        _GrubABPartitionDetecter_path = "app.boot_control.grub.GrubABPartitionDetecter"
        mocker.patch(
            _GrubABPartitionDetecter_path, return_value=_GrubABPartitionDetecter_mock
        )
        # patch reading from /proc/cmdline
        mocker.patch(
            "app.boot_control.grub.cat_proc_cmdline",
            mocker.MagicMock(wraps=self._fsm.cat_proc_cmdline),
        )

    def test_grub_normal_update(self, mocker: pytest_mock.MockerFixture):
        from app.boot_control.grub import GrubController

        _cfg_patch_path = "app.boot_control.grub.cfg"

        ###### stage 1 ######
        # test init from non-ota-partition enabled system

        # mock cfg
        mocker.patch(_cfg_patch_path, self.cfg_for_slot_a_as_current())

        grub_controller = GrubController()
        # TODO: assert normal init
        # TODO: assert symlink
        # TODO: assert grub.cfg

        # test pre-update
        grub_controller.pre_update(
            version=self.UPDATE_VERSION,
            standby_as_ref=False,  # NOTE: not used
            erase_standby=False,  # NOTE: not used
        )

        # NOTE: we have to copy the new kernel files to the slot_b's boot dir
        #       this is done by the create_standby module
        _kernel = f"vmlinuz-{KERNEL_VERSION}"
        _initrd = f"initrd.img-{KERNEL_VERSION}"
        shutil.copy(
            self.slot_a_ota_partition_dir / _kernel, self.slot_b_ota_partition_dir
        )
        shutil.copy(
            self.slot_a_ota_partition_dir / _initrd, self.slot_b_ota_partition_dir
        )

        logger.info("pre-update completed, entering post-update...")
        # test post-update
        grub_controller.post_update()
        # TODO: assert post-update
        # TODO: assert fstab
        # TODO: assert grub.cfg
        # NOTE: check grub.cfg_slot_a_post_update, the target entry is 0
        self._grub_reboot_mock.assert_called_once_with(0)

        ###### stage 2 ######
        # test init after first reboot

        # NOTE: dummy ota-image doesn't have grub installed,
        #       so we need to prepare /etc/default/grub by ourself
        default_grub = self.slot_b / "etc/default/grub"
        default_grub.parent.mkdir(parents=True, exist_ok=True)
        default_grub.write_text(self.DEFAULT_GRUB)

        logger.info("post-update completed, test init after first reboot...")
        # mock cfg
        mocker.patch(_cfg_patch_path, self.cfg_for_slot_b_as_current())

        # test first reboot init
        assert (self.slot_b_ota_partition_dir / "status").read_text() == "UPDATING"
        grub_controller = GrubController()
        assert (self.slot_b_ota_partition_dir / "status").read_text() == "SUCCESS"
        assert self._fsm.is_boot_switched
        # TODO: assert version is updated
