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
"""Performance E2E tests for OTA Client update flow.

This module provides detailed performance measurement for the OTA update flow
from API call to reboot, with bootloader dependencies mocked.

Reports include:
- High precision timing (nanoseconds) for each phase
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from queue import Queue

import pytest
import pytest_mock
from ota_image_libs.v1.image_manifest.schema import ImageIdentifier, OTAReleaseKey

from ota_metadata.utils.cert_store import load_ca_cert_chains, load_ca_store
from otaclient import errors as ota_errors
from otaclient import ota_core
from otaclient._status_monitor import (
    OTAClientStatusCollector,
    OTAStatusChangeReport,
    StatusReport,
)
from otaclient._types import OTAStatus
from otaclient.metrics import OTAMetricsData
from otaclient.ota_core import OTAUpdaterForLegacyOTAImage, OTAUpdaterForOTAImageV1
from otaclient.ota_core._common import create_downloader_pool
from tests.conftest import TestConfiguration as cfg
from tests.utils import SlotMeta

from .conftest import (
    MockBootController,
    MockRebootTriggered,
    PerformanceReport,
    store_report_for_comparison,
)

logger = logging.getLogger(__name__)

OTA_UPDATER_MODULE = ota_core._updater.__name__


@pytest.mark.performance
class TestOTAUpdatePerformanceE2E:
    """Performance E2E tests for OTA update flow with detailed reporting."""

    SESSION_ID = "perf_e2e_test"

    @pytest.fixture
    def prepare_ab_slots(self, tmp_path: Path, ab_slots: SlotMeta):
        """Prepare A/B slots for testing."""
        self.slot_a = Path(ab_slots.slot_a)
        self.slot_b = Path(ab_slots.slot_b)
        self.slot_a_boot_dir = Path(ab_slots.slot_a_boot_dev) / "boot"
        self.slot_b_boot_dir = Path(ab_slots.slot_b_boot_dev) / "boot"
        self.ota_image_dir = Path(cfg.OTA_IMAGE_DIR)
        self.otaclient_run_dir = tmp_path / "otaclient_run_dir"
        self.otaclient_run_dir.mkdir(parents=True, exist_ok=True)

        shutil.rmtree(self.slot_b, ignore_errors=True)
        self.slot_b.mkdir(exist_ok=True)

    @pytest.fixture
    def mock_boot_controller(
        self,
        prepare_ab_slots,
    ) -> MockBootController:
        return MockBootController(
            standby_slot_path=self.slot_b,
            standby_slot_dev=Path("/dev/mock_slot_b"),
            current_version=cfg.CURRENT_VERSION,
            initial_ota_status=OTAStatus.SUCCESS,
        )

    @pytest.fixture(autouse=True)
    def mock_setup(self, mocker: pytest_mock.MockerFixture, prepare_ab_slots):
        mocker.patch(f"{OTA_UPDATER_MODULE}.cfg.ACTIVE_SLOT_MNT", str(self.slot_a))
        mocker.patch(f"{OTA_UPDATER_MODULE}.cfg.STANDBY_SLOT_MNT", str(self.slot_b))
        mocker.patch(f"{OTA_UPDATER_MODULE}.cfg.RUN_DIR", str(self.otaclient_run_dir))
        mocker.patch(f"{OTA_UPDATER_MODULE}.can_use_in_place_mode", return_value=False)
        self._process_persists_mock = mocker.MagicMock()
        mocker.patch(
            f"{OTA_UPDATER_MODULE}.process_persistents", self._process_persists_mock
        )

    def _execute_update_and_collect_metrics(
        self,
        updater,
        report: PerformanceReport,
    ) -> None:
        """Execute update and handle expected MockRebootTriggered exception."""
        # Start execute phase timing (includes all internal phases)
        execute_phase = report.start_phase("execute_total")

        try:
            updater.execute()
            pytest.fail("Expected MockRebootTriggered exception")
        except MockRebootTriggered:
            pass
        except ota_errors.ApplyOTAUpdateFailed as e:
            if isinstance(e.__cause__, MockRebootTriggered):
                pass
            else:
                raise

        execute_phase.end()

        # Extract phase durations from OTAMetricsData timestamps
        # Note: These are in seconds (integer), so precision is limited to 1 second
        # We still report them for reference
        m = updater._metrics

        def ts_to_ns(ts: int) -> int:
            return ts * 1_000_000_000 if ts else 0

        # Create phases from OTAMetricsData timestamps (supplementary, second-precision)
        if (
            m.processing_metadata_start_timestamp
            and m.delta_calculation_start_timestamp
        ):
            phase = report.start_phase("metadata_processing")
            phase.start_ns = ts_to_ns(m.processing_metadata_start_timestamp)
            phase.end_ns = ts_to_ns(m.delta_calculation_start_timestamp)
            phase.extra["note"] = "second-precision from OTAMetricsData"

        if m.delta_calculation_start_timestamp and m.download_start_timestamp:
            phase = report.start_phase("delta_calculation")
            phase.start_ns = ts_to_ns(m.delta_calculation_start_timestamp)
            phase.end_ns = ts_to_ns(m.download_start_timestamp)
            phase.extra["note"] = "second-precision from OTAMetricsData"

        if m.download_start_timestamp and m.apply_update_start_timestamp:
            phase = report.start_phase("download")
            phase.start_ns = ts_to_ns(m.download_start_timestamp)
            phase.end_ns = ts_to_ns(m.apply_update_start_timestamp)
            phase.extra["note"] = "second-precision from OTAMetricsData"

        if m.apply_update_start_timestamp and m.post_update_start_timestamp:
            phase = report.start_phase("apply_update")
            phase.start_ns = ts_to_ns(m.apply_update_start_timestamp)
            phase.end_ns = ts_to_ns(m.post_update_start_timestamp)
            phase.extra["note"] = "second-precision from OTAMetricsData"

        if m.post_update_start_timestamp and m.finalizing_update_start_timestamp:
            phase = report.start_phase("post_update")
            phase.start_ns = ts_to_ns(m.post_update_start_timestamp)
            phase.end_ns = ts_to_ns(m.finalizing_update_start_timestamp)
            phase.extra["note"] = "second-precision from OTAMetricsData"

        if m.finalizing_update_start_timestamp and m.reboot_start_timestamp:
            phase = report.start_phase("finalization")
            phase.start_ns = ts_to_ns(m.finalizing_update_start_timestamp)
            phase.end_ns = ts_to_ns(m.reboot_start_timestamp)
            phase.extra["note"] = "second-precision from OTAMetricsData"

    def test_legacy_ota_image_performance(
        self,
        ota_status_collector: tuple[OTAClientStatusCollector, Queue[StatusReport]],
        mocker: pytest_mock.MockerFixture,
        tmp_path: Path,
        mock_boot_controller: MockBootController,
    ) -> None:
        """Performance test for Legacy OTA image update flow."""
        report = PerformanceReport(
            test_name="test_legacy_ota_image_performance",
            ota_image_format="legacy",
        )

        _, report_queue = ota_status_collector
        ecu_status_flags = mocker.MagicMock()
        ecu_status_flags.any_child_ecu_in_update.is_set = mocker.MagicMock(
            return_value=False
        )
        critical_zone_flag = mocker.MagicMock()
        critical_zone_flag.acquire_lock_with_release.return_value.__enter__ = (
            mocker.MagicMock(return_value=True)
        )
        critical_zone_flag.acquire_lock_with_release.return_value.__exit__ = (
            mocker.MagicMock(return_value=False)
        )
        abort_ota_flag = mocker.MagicMock()
        abort_ota_flag.shutdown_requested = mocker.MagicMock()
        abort_ota_flag.shutdown_requested.is_set.return_value = False
        abort_ota_flag.shutdown_requested.wait.return_value = False
        abort_ota_flag.reject_abort = mocker.MagicMock()
        abort_ota_flag.reject_abort.is_set.return_value = False
        abort_ota_flag.abort_acknowledged = mocker.MagicMock()
        abort_ota_flag.abort_acknowledged.wait.return_value = False
        abort_ota_flag.status_written = mocker.MagicMock()

        # Start test timing
        report.start_test()
        report.record_status(OTAStatus.UPDATING)

        ca_chains_store = load_ca_cert_chains(cfg.CERTS_DIR)
        downloader_pool = create_downloader_pool(
            raw_cookies_json=cfg.COOKIES_JSON,
            download_threads=3,
            chunk_size=1024**2,
        )
        report_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(new_ota_status=OTAStatus.UPDATING),
                session_id=self.SESSION_ID,
            )
        )
        session_workdir = tmp_path / "session_workdir"

        _updater = OTAUpdaterForLegacyOTAImage(
            version=cfg.UPDATE_VERSION,
            raw_url_base=cfg.OTA_IMAGE_URL,
            session_wd=session_workdir,
            ca_chains_store=ca_chains_store,
            downloader_pool=downloader_pool,
            boot_controller=mock_boot_controller,  # type: ignore
            ecu_status_flags=ecu_status_flags,
            critical_zone_flag=critical_zone_flag,
            abort_ota_flag=abort_ota_flag,
            session_id=self.SESSION_ID,
            status_report_queue=report_queue,
            metrics=OTAMetricsData(),
            shm_metrics_reader=None,  # type: ignore
        )

        # Execute update
        self._execute_update_and_collect_metrics(_updater, report)

        # End test timing
        report.end_test()

        # Log detailed report
        report_text = report.generate_report()
        logger.info(report_text)
        print(report_text)  # Also print for immediate visibility

        # Also log JSON for programmatic analysis
        json_report = report.to_json()
        logger.info(f"\n[JSON Report]\n{json_report}")
        print(f"\n[JSON Report]\n{json_report}")

        # Store for comparison report
        store_report_for_comparison(report)

        # Assertions
        assert _updater.update_version == str(cfg.UPDATE_VERSION)
        self._process_persists_mock.assert_called_once()

    def test_ota_image_v1_performance(
        self,
        ota_status_collector: tuple[OTAClientStatusCollector, Queue[StatusReport]],
        mocker: pytest_mock.MockerFixture,
        tmp_path: Path,
        mock_boot_controller: MockBootController,
    ) -> None:
        """Performance test for OTA Image V1 update flow."""
        report = PerformanceReport(
            test_name="test_ota_image_v1_performance",
            ota_image_format="v1",
        )

        _, report_queue = ota_status_collector
        ecu_status_flags = mocker.MagicMock()
        ecu_status_flags.any_child_ecu_in_update.is_set = mocker.MagicMock(
            return_value=False
        )
        critical_zone_flag = mocker.MagicMock()
        critical_zone_flag.acquire_lock_with_release.return_value.__enter__ = (
            mocker.MagicMock(return_value=True)
        )
        critical_zone_flag.acquire_lock_with_release.return_value.__exit__ = (
            mocker.MagicMock(return_value=False)
        )
        abort_ota_flag = mocker.MagicMock()
        abort_ota_flag.shutdown_requested = mocker.MagicMock()
        abort_ota_flag.shutdown_requested.is_set.return_value = False
        abort_ota_flag.shutdown_requested.wait.return_value = False
        abort_ota_flag.reject_abort = mocker.MagicMock()
        abort_ota_flag.reject_abort.is_set.return_value = False
        abort_ota_flag.abort_acknowledged = mocker.MagicMock()
        abort_ota_flag.abort_acknowledged.wait.return_value = False
        abort_ota_flag.status_written = mocker.MagicMock()

        # Start test timing
        report.start_test()
        report.record_status(OTAStatus.UPDATING)

        ca_store = load_ca_store(cfg.CERTS_OTA_IMAGE_V1_DIR)
        downloader_pool = create_downloader_pool(
            raw_cookies_json=cfg.COOKIES_JSON,
            download_threads=3,
            chunk_size=1024**2,
        )
        report_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(new_ota_status=OTAStatus.UPDATING),
                session_id=self.SESSION_ID,
            )
        )
        session_workdir = tmp_path / "session_workdir"

        _updater = OTAUpdaterForOTAImageV1(
            version=cfg.UPDATE_VERSION,
            raw_url_base=cfg.OTA_IMAGE_V1_URL,
            session_wd=session_workdir,
            ca_store=ca_store,
            downloader_pool=downloader_pool,
            boot_controller=mock_boot_controller,  # type: ignore
            ecu_status_flags=ecu_status_flags,
            critical_zone_flag=critical_zone_flag,
            abort_ota_flag=abort_ota_flag,
            session_id=self.SESSION_ID,
            status_report_queue=report_queue,
            metrics=OTAMetricsData(),
            image_identifier=ImageIdentifier("autoware", OTAReleaseKey.dev),
            shm_metrics_reader=None,  # type: ignore
        )

        # Execute update
        self._execute_update_and_collect_metrics(_updater, report)

        # End test timing
        report.end_test()

        # Log detailed report
        report_text = report.generate_report()
        logger.info(report_text)
        print(report_text)  # Also print for immediate visibility

        json_report = report.to_json()
        logger.info(f"\n[JSON Report]\n{json_report}")
        print(f"\n[JSON Report]\n{json_report}")

        # Store for comparison report
        store_report_for_comparison(report)

        # Assertions
        assert _updater.update_version == str(cfg.UPDATE_VERSION)
        self._process_persists_mock.assert_called_once()
