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
"""Keeping why an OTA failed across the reboot that ended it.

A failure before the reboot is reported from the memory of the process that hit it.
A trial boot that does not come up healthy is reported by the next boot, and by then
that process is gone: the status file says FAILURE and the fleet learns nothing else.
These tests are about what is left beside the status file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from otaclient._types import FailureType, OTAStatus
from otaclient.boot_control._ota_status_control import OTAStatusFilesControl
from otaclient.configs.cfg import cfg

SLOT_A, SLOT_B = "slot_a", "slot_b"


def _control(
    tmp_path: Path, *, finalize=lambda: True, **kwargs
) -> OTAStatusFilesControl:
    return OTAStatusFilesControl(
        active_slot=SLOT_A,
        standby_slot=SLOT_B,
        current_ota_status_dir=tmp_path / SLOT_A,
        standby_ota_status_dir=tmp_path / SLOT_B,
        finalize_switching_boot=finalize,
        **kwargs,
    )


def _write(tmp_path: Path, slot: str, name: str, value: str) -> None:
    _d = tmp_path / slot
    _d.mkdir(parents=True, exist_ok=True)
    (_d / name).write_text(value)


class TestRecordedFailure:
    def test_a_recorded_failure_is_read_back(self, tmp_path: Path):
        _ctrl = _control(tmp_path)
        _ctrl.store_failure(
            FailureType.UNRECOVERABLE, "the standby slot is smaller than the image"
        )

        assert _control(tmp_path).load_failure() == (
            FailureType.UNRECOVERABLE,
            "the standby slot is smaller than the image",
        )

    def test_nothing_recorded_reads_as_nothing(self, tmp_path: Path):
        assert _control(tmp_path).load_failure() is None

    def test_a_record_that_cannot_be_read_is_not_reported(self, tmp_path: Path):
        """A truncated write is not a reason to fail on the next boot as well."""
        _ctrl = _control(tmp_path)
        (_ctrl.current_ota_status_dir / cfg.OTA_FAILURE_FNAME).write_text("{ not json")

        assert _ctrl.load_failure() is None

    def test_a_success_clears_it(self, tmp_path: Path):
        """Otherwise the reason of an OTA that failed is reported against the one that
        came after it and worked."""
        _ctrl = _control(tmp_path)
        _ctrl.store_failure(FailureType.RECOVERABLE, "the link dropped")

        _ctrl._store_current_status(OTAStatus.SUCCESS)

        assert _ctrl.load_failure() is None


class TestFailuresTheRebootCaused:
    def test_a_trial_boot_that_came_back_on_the_old_slot_says_so(self, tmp_path: Path):
        """The update was staged, the machine rebooted, and here it is on the slot it
        started from: the trial boot did not come up healthy, or never got that far.
        Nothing in memory survived to say it, so the boot that finds it records it."""
        _write(tmp_path, SLOT_A, cfg.OTA_STATUS_FNAME, OTAStatus.UPDATING.name)
        _write(tmp_path, SLOT_A, cfg.SLOT_IN_USE_FNAME, SLOT_B)  # we should be on B

        _ctrl = _control(tmp_path)

        assert _ctrl.booted_ota_status == OTAStatus.FAILURE
        _failure = _ctrl.load_failure()
        assert _failure is not None
        _type, _reason = _failure
        assert _type is FailureType.RECOVERABLE, (
            "the slot that boots is the old one, intact"
        )
        assert "not in switching boot mode" in _reason

    def test_a_finalization_that_failed_says_so(self, tmp_path: Path):
        """On the staged slot, but the platform would not commit it."""
        _write(tmp_path, SLOT_A, cfg.OTA_STATUS_FNAME, OTAStatus.UPDATING.name)
        _write(tmp_path, SLOT_A, cfg.SLOT_IN_USE_FNAME, SLOT_A)  # switching boot

        _ctrl = _control(tmp_path, finalize=lambda: False)

        assert _ctrl.booted_ota_status == OTAStatus.FAILURE
        _failure = _ctrl.load_failure()
        assert _failure is not None
        assert "finalization failed" in _failure[1]

    def test_a_commit_leaves_no_reason_behind(self, tmp_path: Path):
        _write(tmp_path, SLOT_A, cfg.OTA_STATUS_FNAME, OTAStatus.UPDATING.name)
        _write(tmp_path, SLOT_A, cfg.SLOT_IN_USE_FNAME, SLOT_A)

        _ctrl = _control(tmp_path, finalize=lambda: True)

        assert _ctrl.booted_ota_status == OTAStatus.SUCCESS
        assert _ctrl.load_failure() is None


@pytest.mark.parametrize("status", [OTAStatus.FAILURE, OTAStatus.ROLLBACK_FAILURE])
def test_a_failure_status_keeps_its_reason(tmp_path: Path, status: OTAStatus):
    _ctrl = _control(tmp_path)
    _ctrl.store_failure(FailureType.UNRECOVERABLE, "why")

    _ctrl._store_current_status(status)

    assert _ctrl.load_failure() == (FailureType.UNRECOVERABLE, "why")
