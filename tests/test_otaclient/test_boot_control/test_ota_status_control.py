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
import threading
from functools import partial
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from otaclient._types import OTAStatus
from otaclient.boot_control._ota_status_control import OTAStatusFilesControl
from otaclient.configs.cfg import cfg as otaclient_cfg
from otaclient.metrics import OTAMetricsData
from otaclient_common._io import read_str_from_file, write_str_to_file_atomic

logger = logging.getLogger(__name__)


def _dummy_finalize_switch_boot(
    flag: threading.Event, boot_control_switch_boot_result: bool
):
    flag.set()
    return boot_control_switch_boot_result


class TestOTAStatusFilesControl:
    SLOT_A_ID, SLOT_B_ID = "slot_a", "slot_b"

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path):
        self.slot_a, self.slot_b = self.SLOT_A_ID, self.SLOT_B_ID
        self.slot_a_ota_status_dir = tmp_path / "slot_a_ota_status_dir"
        self.slot_a_ota_status_dir.mkdir()
        self.slot_b_ota_status_dir = tmp_path / "slot_b_ota_status_dir"
        self.slot_b_ota_status_dir.mkdir()

        self.slot_a_status_file = (
            self.slot_a_ota_status_dir / otaclient_cfg.OTA_STATUS_FNAME
        )
        self.slot_b_status_file = (
            self.slot_b_ota_status_dir / otaclient_cfg.OTA_STATUS_FNAME
        )
        self.slot_a_slot_in_use_file = (
            self.slot_a_ota_status_dir / otaclient_cfg.SLOT_IN_USE_FNAME
        )
        self.slot_b_slot_in_use_file = (
            self.slot_b_ota_status_dir / otaclient_cfg.SLOT_IN_USE_FNAME
        )

        self.finalize_switch_boot_flag = threading.Event()
        self.finalize_switch_boot_func = partial(
            _dummy_finalize_switch_boot, self.finalize_switch_boot_flag
        )

    @pytest.mark.parametrize(
        (
            "test_case,input_slot_a_status,input_slot_a_slot_in_use,force_initialize,"
            "output_slot_a_status,output_slot_a_slot_in_use"
        ),
        (
            (
                "test_initialize",
                # input
                None,
                "",
                False,
                # output
                OTAStatus.INITIALIZED,
                SLOT_A_ID,
            ),
            (
                "test_force_initialize",
                # input
                OTAStatus.SUCCESS,
                SLOT_A_ID,
                True,
                # output
                OTAStatus.INITIALIZED,
                SLOT_A_ID,
            ),
            (
                "test_normal_boot",
                # input
                OTAStatus.SUCCESS,
                SLOT_A_ID,
                False,
                # output
                OTAStatus.SUCCESS,
                SLOT_A_ID,
            ),
        ),
    )
    def test_ota_status_files_loading(
        self,
        test_case: str,
        input_slot_a_status: Optional[OTAStatus],
        input_slot_a_slot_in_use: str,
        force_initialize: bool,
        output_slot_a_status: OTAStatus,
        output_slot_a_slot_in_use: str,
    ):
        logger.info(f"{test_case=}")
        # ------ setup ------ #
        write_str_to_file_atomic(
            self.slot_a_status_file,
            input_slot_a_status if input_slot_a_status else "",
        )
        write_str_to_file_atomic(self.slot_a_slot_in_use_file, input_slot_a_slot_in_use)

        # ------ execution ------ #
        status_control = OTAStatusFilesControl(
            active_slot=self.slot_a,
            standby_slot=self.slot_b,
            current_ota_status_dir=self.slot_a_ota_status_dir,
            standby_ota_status_dir=self.slot_b_ota_status_dir,
            finalize_switching_boot=partial(self.finalize_switch_boot_func, True),
            force_initialize=force_initialize,
        )

        # ------ assertion ------ #
        assert not self.finalize_switch_boot_flag.is_set()
        # check slot a
        assert read_str_from_file(self.slot_a_status_file) == output_slot_a_status
        assert status_control.booted_ota_status == output_slot_a_status
        assert (
            read_str_from_file(self.slot_a_slot_in_use_file)
            == status_control._load_current_slot_in_use()
            == output_slot_a_slot_in_use
        )

    def test_pre_update(self):
        """Test update from slot_a to slot_b."""
        # ------ direct init ------ #
        status_control = OTAStatusFilesControl(
            active_slot=self.slot_a,
            standby_slot=self.slot_b,
            current_ota_status_dir=self.slot_a_ota_status_dir,
            standby_ota_status_dir=self.slot_b_ota_status_dir,
            finalize_switching_boot=partial(self.finalize_switch_boot_func, True),
            force_initialize=False,
        )

        # ------ execution ------ #
        status_control.pre_update_current()

        # ------ assertion ------ #
        assert not self.finalize_switch_boot_flag.is_set()
        # slot_a: current slot
        assert read_str_from_file(self.slot_a_status_file) == OTAStatus.FAILURE
        assert (
            read_str_from_file(self.slot_a_slot_in_use_file)
            == status_control._load_current_slot_in_use()
            == self.slot_b
        )

    def test_post_update(self):
        # ------ direct init ------ #
        status_control = OTAStatusFilesControl(
            active_slot=self.slot_a,
            standby_slot=self.slot_b,
            current_ota_status_dir=self.slot_a_ota_status_dir,
            standby_ota_status_dir=self.slot_b_ota_status_dir,
            finalize_switching_boot=partial(self.finalize_switch_boot_func, True),
            force_initialize=False,
        )

        # ------ execution ------ #
        status_control.post_update_standby(version="dummy_version")

        # ------ assertion ------ #
        assert not self.finalize_switch_boot_flag.is_set()
        # slot_b: standby slot
        assert read_str_from_file(self.slot_b_status_file) == OTAStatus.UPDATING
        assert read_str_from_file(self.slot_b_slot_in_use_file) == self.slot_b

    @pytest.mark.parametrize(
        ("test_case,finalizing_result"),
        (
            (
                "test_finalizing_failed",
                False,
            ),
            (
                "test_finalizing_succeeded",
                True,
            ),
        ),
    )
    def test_switching_boot(
        self,
        test_case: str,
        finalizing_result: bool,
    ):
        """First reboot after OTA from slot_a to slot_b."""
        logger.info(f"{test_case=}")
        # ------ setup ------ #
        write_str_to_file_atomic(self.slot_a_status_file, OTAStatus.FAILURE)
        write_str_to_file_atomic(self.slot_a_slot_in_use_file, self.slot_b)
        write_str_to_file_atomic(self.slot_b_status_file, OTAStatus.UPDATING)
        write_str_to_file_atomic(self.slot_b_slot_in_use_file, self.slot_b)

        # ------ execution ------ #
        # otaclient boots on slot_b
        status_control = OTAStatusFilesControl(
            active_slot=self.slot_b,
            standby_slot=self.slot_a,
            current_ota_status_dir=self.slot_b_ota_status_dir,
            standby_ota_status_dir=self.slot_a_ota_status_dir,
            finalize_switching_boot=partial(
                self.finalize_switch_boot_func, finalizing_result
            ),
            force_initialize=False,
        )

        # ------ assertion ------ #
        # ensure finalizing is called
        assert self.finalize_switch_boot_flag.is_set()

        # check slot a
        assert read_str_from_file(self.slot_a_status_file) == OTAStatus.FAILURE
        assert (
            read_str_from_file(self.slot_a_slot_in_use_file)
            == status_control._load_current_slot_in_use()
            == self.slot_b
        )
        assert (
            read_str_from_file(self.slot_b_slot_in_use_file)
            == status_control._load_current_slot_in_use()
            == self.slot_b
        )

        # finalizing succeeded
        if finalizing_result:
            assert status_control.booted_ota_status == OTAStatus.SUCCESS
            assert read_str_from_file(self.slot_b_status_file) == OTAStatus.SUCCESS

        else:
            assert status_control.booted_ota_status == OTAStatus.FAILURE
            assert read_str_from_file(self.slot_b_status_file) == OTAStatus.FAILURE

    def test_accidentally_boots_back_to_standby(self):
        """slot_a should be active slot but boots back to slot_b."""
        # ------ setup ------ #
        write_str_to_file_atomic(self.slot_a_status_file, OTAStatus.SUCCESS)
        write_str_to_file_atomic(self.slot_a_slot_in_use_file, self.slot_a)
        write_str_to_file_atomic(self.slot_b_status_file, OTAStatus.FAILURE)
        write_str_to_file_atomic(self.slot_b_slot_in_use_file, self.slot_a)

        # ------ execution ------ #
        # otaclient accidentally boots on slot_b
        status_control = OTAStatusFilesControl(
            active_slot=self.slot_b,
            standby_slot=self.slot_a,
            current_ota_status_dir=self.slot_b_ota_status_dir,
            standby_ota_status_dir=self.slot_a_ota_status_dir,
            finalize_switching_boot=partial(self.finalize_switch_boot_func, True),
            force_initialize=False,
        )

        # ------ assertion ------ #
        assert not self.finalize_switch_boot_flag.is_set()
        # slot_b's status is read
        assert status_control.booted_ota_status == OTAStatus.FAILURE

    # NOTE(20251017): as we separate the downloaded dynamic loading app and
    #                 systemd managed otaclient app image, change to check
    #                 is_running_as_downloaded_dynamic_app here.
    @pytest.mark.parametrize(
        "initial_status",
        [
            OTAStatus.FAILURE,
            OTAStatus.SUCCESS,
        ],
    )
    @patch(
        "otaclient.boot_control._ota_status_control._env.is_running_as_downloaded_dynamic_app",
        return_value=True,
    )
    def test_dynamic_client_running_status_handling(
        self, mock_is_dynamic_client_running, initial_status: OTAStatus
    ):
        """Test that any status is converted to SUCCESS when dynamic client is running."""
        # ------ setup ------ #
        write_str_to_file_atomic(self.slot_a_status_file, initial_status)
        write_str_to_file_atomic(self.slot_a_slot_in_use_file, self.slot_a)

        # ------ execution ------ #
        status_control = OTAStatusFilesControl(
            active_slot=self.slot_a,
            standby_slot=self.slot_b,
            current_ota_status_dir=self.slot_a_ota_status_dir,
            standby_ota_status_dir=self.slot_b_ota_status_dir,
            finalize_switching_boot=partial(self.finalize_switch_boot_func, True),
            force_initialize=False,
        )

        # ------ assertion ------ #
        assert not self.finalize_switch_boot_flag.is_set()
        # Any status should be converted to SUCCESS when dynamic client is running
        assert status_control.booted_ota_status == OTAStatus.SUCCESS
        # The original status file should remain unchanged
        assert read_str_from_file(self.slot_a_status_file) == initial_status
        assert (
            read_str_from_file(self.slot_a_slot_in_use_file)
            == status_control._load_current_slot_in_use()
            == self.slot_a
        )

    def test_store_and_load_metrics(self):
        """Test metrics storage and loading functionality."""
        status_control = OTAStatusFilesControl(
            active_slot=self.slot_a,
            standby_slot=self.slot_b,
            current_ota_status_dir=self.slot_a_ota_status_dir,
            standby_ota_status_dir=self.slot_b_ota_status_dir,
            finalize_switching_boot=partial(self.finalize_switch_boot_func, True),
        )

        # Create sample metrics
        metrics = OTAMetricsData(
            session_id="test-session-123",
            ota_image_total_files_size=1000000,
            delta_download_files_num=100,
        )

        # Store metrics
        status_control._store_current_metrics(metrics)

        # Load metrics
        loaded_metrics = status_control._load_current_metrics()

        # Verify metrics were correctly stored and loaded
        assert loaded_metrics is not None
        assert loaded_metrics.session_id == metrics.session_id
        assert (
            loaded_metrics.ota_image_total_files_size
            == metrics.ota_image_total_files_size
        )
        assert (
            loaded_metrics.delta_download_files_num == metrics.delta_download_files_num
        )

    def test_load_metrics_no_file(self):
        """Test loading metrics when file doesn't exist."""
        status_control = OTAStatusFilesControl(
            active_slot=self.slot_a,
            standby_slot=self.slot_b,
            current_ota_status_dir=self.slot_a_ota_status_dir,
            standby_ota_status_dir=self.slot_b_ota_status_dir,
            finalize_switching_boot=partial(self.finalize_switch_boot_func, True),
        )

        # Load metrics when no file exists
        loaded_metrics = status_control._load_current_metrics()

        # Should return None when file doesn't exist
        assert loaded_metrics is None

    @pytest.mark.parametrize(
        "ota_status",
        [OTAStatus.UPDATING, OTAStatus.ROLLBACKING],
    )
    def test_publish_metrics_after_reboot(self, ota_status: OTAStatus):
        """Test metrics publication after reboot for UPDATING/ROLLBACKING states."""
        # Create metrics file before initializing status control
        metrics = OTAMetricsData(
            session_id="test-session-456",
            ota_image_total_files_size=2000000,
            delta_download_files_num=200,
        )

        # Manually write metrics file
        metrics_file = self.slot_a_ota_status_dir / otaclient_cfg.METRICS_FNAME
        write_str_to_file_atomic(metrics_file, metrics.to_json())

        # Write status file to simulate post-reboot state
        write_str_to_file_atomic(self.slot_a_status_file, ota_status.name)
        write_str_to_file_atomic(self.slot_a_slot_in_use_file, self.slot_a)

        with patch.object(OTAMetricsData, "publish") as mock_publish:
            _ = OTAStatusFilesControl(
                active_slot=self.slot_a,
                standby_slot=self.slot_b,
                current_ota_status_dir=self.slot_a_ota_status_dir,
                standby_ota_status_dir=self.slot_b_ota_status_dir,
                finalize_switching_boot=partial(self.finalize_switch_boot_func, True),
            )

            # Verify publish was called
            mock_publish.assert_called_once()
            # Get the metrics instance that was published
            # Since publish is called on the metrics instance, we can verify it was called

    @pytest.mark.parametrize(
        "ota_status",
        [OTAStatus.SUCCESS, OTAStatus.FAILURE],
    )
    def test_no_publish_metrics_for_non_updating_status(self, ota_status: OTAStatus):
        """Test that metrics are not published for SUCCESS/FAILURE states."""
        # Create metrics file
        metrics = OTAMetricsData(
            session_id="test-session-789",
            ota_image_total_files_size=3000000,
            delta_download_files_num=300,
        )

        metrics_file = self.slot_a_ota_status_dir / otaclient_cfg.METRICS_FNAME
        write_str_to_file_atomic(metrics_file, metrics.to_json())

        # Write status file
        write_str_to_file_atomic(self.slot_a_status_file, ota_status.name)
        write_str_to_file_atomic(self.slot_a_slot_in_use_file, self.slot_a)

        with patch.object(OTAMetricsData, "publish") as mock_publish:
            _ = OTAStatusFilesControl(
                active_slot=self.slot_a,
                standby_slot=self.slot_b,
                current_ota_status_dir=self.slot_a_ota_status_dir,
                standby_ota_status_dir=self.slot_b_ota_status_dir,
                finalize_switching_boot=partial(self.finalize_switch_boot_func, True),
            )

            # Verify publish was NOT called
            mock_publish.assert_not_called()

    def test_publish_metrics_with_missing_file(self):
        """Test that missing metrics file doesn't cause errors during initialization."""
        # Write status file for UPDATING state
        write_str_to_file_atomic(self.slot_a_status_file, OTAStatus.UPDATING.name)
        write_str_to_file_atomic(self.slot_a_slot_in_use_file, self.slot_a)

        with patch.object(OTAMetricsData, "publish") as mock_publish:
            _ = OTAStatusFilesControl(
                active_slot=self.slot_a,
                standby_slot=self.slot_b,
                current_ota_status_dir=self.slot_a_ota_status_dir,
                standby_ota_status_dir=self.slot_b_ota_status_dir,
                finalize_switching_boot=partial(self.finalize_switch_boot_func, True),
            )

            # Verify publish was NOT called when file is missing
            mock_publish.assert_not_called()
