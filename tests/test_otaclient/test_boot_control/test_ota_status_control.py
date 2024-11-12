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

import pytest

from otaclient._types import OTAStatus
from otaclient.boot_control._common import OTAStatusFilesControl
from otaclient.configs.cfg import cfg as otaclient_cfg
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
        status_control.pre_update_standby(version="dummy_version")

        # ------ assertion ------ #
        assert not self.finalize_switch_boot_flag.is_set()
        # slot_a: current slot
        assert read_str_from_file(self.slot_a_status_file) == OTAStatus.FAILURE
        assert (
            read_str_from_file(self.slot_a_slot_in_use_file)
            == status_control._load_current_slot_in_use()
            == self.slot_b
        )
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
