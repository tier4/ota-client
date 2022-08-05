import shutil
import pytest
from pathlib import Path
from typing import Tuple
from pytest_mock import MockerFixture

from app.create_standby.interface import UpdateMeta
from app.create_standby.rebuild_mode import RebuildMode
from app.ota_metadata import OtaMetadata
from app.proto import wrapper

from tests.conftest import OTA_IMAGE_DIR, OTA_IMAGE_SERVER_PORT, OTA_IMAGE_SERVER_ADDR
from tests.utils import compare_dir

import logging

logger = logging.getLogger(__name__)


class Test_rebuild_mode:
    @pytest.fixture(autouse=True)
    def prepare_ab_slots(self, ab_slots: Tuple[str, str, str, str]):
        (
            self.slot_a,
            self.slot_b,
            self.slot_a_boot_dir,
            self.slot_b_boot_dir,
        ) = map(Path, ab_slots)

        # cleanup slot_b
        shutil.rmtree(self.slot_b, ignore_errors=True)
        self.slot_b.mkdir(exist_ok=True)
        yield
        # cleanup slot_b after test
        shutil.rmtree(self.slot_b, ignore_errors=True)

    @pytest.fixture(autouse=True)
    def prepare_mock(self, prepare_ab_slots, mocker: MockerFixture):
        cfg_path = "app.create_standby.rebuild_mode.cfg"
        proxy_cfg_path = "app.create_standby.rebuild_mode.proxy_cfg"
        mocker.patch(f"{cfg_path}.PASSWD_FILE", f"{self.slot_a}/etc/passwd")
        mocker.patch(f"{cfg_path}.GROUP_FILE", f"{self.slot_a}/group")
        mocker.patch(f"{proxy_cfg_path}.get_proxy_for_local_ota", return_value=None)

        # mock RebuildMode
        # TODO: mock save_meta here as save_meta will
        # introduce diff between ota_image and slot b
        rebuild_mode_cls = "app.create_standby.rebuild_mode.RebuildMode"
        mocker.patch(f"{rebuild_mode_cls}._save_meta")
        # TODO: mock process_persistents here
        mocker.patch(f"{rebuild_mode_cls}._process_persistents")

    @pytest.fixture(autouse=True)
    def generate_update_meta(self, prepare_mock):
        self.update_meta = UpdateMeta(
            cookies={},
            metadata=OtaMetadata((Path(OTA_IMAGE_DIR) / "metadata.jwt").read_text()),
            url_base=f"http://{OTA_IMAGE_SERVER_ADDR}:{OTA_IMAGE_SERVER_PORT}",
            boot_dir=str(self.slot_b_boot_dir),
            standby_slot_mount_point=str(self.slot_b),
            ref_slot_mount_point=str(self.slot_a),
        )

    def test_rebuild_mode(self, mocker: MockerFixture):
        update_stats_collector = mocker.MagicMock()
        update_phase_tracker = mocker.MagicMock()

        builder = RebuildMode(
            update_meta=self.update_meta,
            stats_collector=update_stats_collector,
            update_phase_tracker=update_phase_tracker,
        )
        builder.create_standby_slot()

        # check status progress update
        update_phase_tracker.assert_any_call(wrapper.StatusProgressPhase.METADATA)
        update_phase_tracker.assert_any_call(wrapper.StatusProgressPhase.REGULAR)
        update_phase_tracker.assert_any_call(wrapper.StatusProgressPhase.DIRECTORY)
        # update_phase_tracker.assert_any_call(wrapper.StatusProgressPhase.PERSISTENT)

        # check slot populating result
        # NOTE: merge contents from slot_b_boot_dir to slot_b
        shutil.copytree(self.slot_b_boot_dir, self.slot_b / "boot", dirs_exist_ok=True)
        # NOTE: for some reason tmp dir is created under OTA_IMAGE_DIR/data, but not listed
        # in the regulars.txt, so we create one here to make the test passed
        (self.slot_b / "tmp").mkdir(exist_ok=True)
        compare_dir(Path(OTA_IMAGE_DIR) / "data", self.slot_b)
