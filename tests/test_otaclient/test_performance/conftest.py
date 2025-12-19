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
"""Fixtures for performance E2E tests with high-precision timing."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from otaclient._types import OTAStatus

logger = logging.getLogger(__name__)


@dataclass
class MockBootControllerTimings:
    """Track timings for mock boot controller operations with nanosecond precision."""

    pre_update_start_ns: int = 0
    pre_update_end_ns: int = 0
    post_update_start_ns: int = 0
    post_update_end_ns: int = 0
    finalizing_update_start_ns: int = 0
    finalizing_update_end_ns: int = 0
    finalizing_update_called: bool = False
    reboot_triggered: bool = False

    @property
    def pre_update_duration_ms(self) -> float:
        return (self.pre_update_end_ns - self.pre_update_start_ns) / 1_000_000

    @property
    def post_update_duration_ms(self) -> float:
        return (self.post_update_end_ns - self.post_update_start_ns) / 1_000_000

    @property
    def finalizing_update_duration_ms(self) -> float:
        return (
            self.finalizing_update_end_ns - self.finalizing_update_start_ns
        ) / 1_000_000


@dataclass
class PhaseMetrics:
    """High-precision metrics for a single phase."""

    name: str
    start_ns: int = 0
    end_ns: int = 0

    # Optional detailed metrics
    files_processed: int = 0
    bytes_processed: int = 0
    errors: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def start(self) -> None:
        self.start_ns = time.time_ns()

    def end(self) -> None:
        self.end_ns = time.time_ns()

    @property
    def duration_ns(self) -> int:
        return self.end_ns - self.start_ns

    @property
    def duration_us(self) -> float:
        return self.duration_ns / 1_000

    @property
    def duration_ms(self) -> float:
        return self.duration_ns / 1_000_000

    @property
    def duration_s(self) -> float:
        return self.duration_ns / 1_000_000_000

    @property
    def throughput_mb_s(self) -> float:
        if self.bytes_processed > 0 and self.duration_s > 0:
            return (self.bytes_processed / (1024 * 1024)) / self.duration_s
        return 0.0

    @property
    def files_per_s(self) -> float:
        if self.files_processed > 0 and self.duration_s > 0:
            return self.files_processed / self.duration_s
        return 0.0


@dataclass
class PerformanceReport:
    """Detailed performance report with high-precision timing."""

    test_name: str = ""
    ota_image_format: str = ""  # "legacy" or "v1"
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    # Overall timing
    test_start_ns: int = 0
    test_end_ns: int = 0

    # Phases
    phases: dict[str, PhaseMetrics] = field(default_factory=dict)

    # Boot controller timings
    boot_timings: MockBootControllerTimings = field(
        default_factory=MockBootControllerTimings
    )

    # OTA Image stats from OTAMetricsData
    total_regulars_num: int = 0
    total_directories_num: int = 0
    total_symlinks_num: int = 0
    total_files_size: int = 0
    delta_download_files_num: int = 0
    delta_download_files_size: int = 0
    downloaded_bytes: int = 0
    downloaded_errors: int = 0

    # Status transitions
    status_transitions: list[tuple[int, str]] = field(default_factory=list)

    def start_test(self) -> None:
        self.test_start_ns = time.time_ns()

    def end_test(self) -> None:
        self.test_end_ns = time.time_ns()

    @property
    def total_duration_ns(self) -> int:
        return self.test_end_ns - self.test_start_ns

    @property
    def total_duration_ms(self) -> float:
        return self.total_duration_ns / 1_000_000

    @property
    def total_duration_s(self) -> float:
        return self.total_duration_ns / 1_000_000_000

    def start_phase(self, name: str) -> PhaseMetrics:
        phase = PhaseMetrics(name=name)
        phase.start()
        self.phases[name] = phase
        return phase

    def end_phase(self, name: str) -> None:
        if name in self.phases:
            self.phases[name].end()

    def record_status(self, status: OTAStatus) -> None:
        self.status_transitions.append((time.time_ns(), status.name))

    def generate_report(self) -> str:
        """Generate detailed performance report."""
        lines = [
            "",
            "=" * 100,
            "  PERFORMANCE E2E TEST REPORT",
            "=" * 100,
            f"  Test: {self.test_name}",
            f"  OTA Image Format: {self.ota_image_format}",
            f"  Timestamp: {self.timestamp}",
            "",
            f"  TOTAL DURATION: {self.total_duration_ms:,.3f} ms ({self.total_duration_s:.6f} s)",
            "",
            "-" * 100,
            "  PHASE BREAKDOWN (High Precision)",
            "-" * 100,
        ]

        phase_order = [
            "updater_init",
            "execute_total",
            "metadata_processing",
            "delta_calculation",
            "download",
            "apply_update",
            "post_update",
            "finalization",
        ]

        total_tracked = 0
        for phase_name in phase_order:
            if phase_name in self.phases:
                p = self.phases[phase_name]
                total_tracked += p.duration_ns
                lines.append(f"  {phase_name:25s}: {p.duration_ms:>12,.3f} ms")
                if p.files_processed > 0:
                    lines.append(
                        f"    {'files':23s}: {p.files_processed:>12,} ({p.files_per_s:,.1f}/s)"
                    )
                if p.bytes_processed > 0:
                    mb = p.bytes_processed / (1024 * 1024)
                    lines.append(
                        f"    {'bytes':23s}: {p.bytes_processed:>12,} ({mb:,.2f} MB, {p.throughput_mb_s:.2f} MB/s)"
                    )
                if p.errors > 0:
                    lines.append(f"    {'errors':23s}: {p.errors:>12,}")
                for k, v in p.extra.items():
                    lines.append(f"    {k:23s}: {v}")

        # Untracked time
        overhead_ns = self.total_duration_ns - total_tracked
        overhead_ms = overhead_ns / 1_000_000
        lines.append(f"  {'(overhead/other)':25s}: {overhead_ms:>12,.3f} ms")

        # OTA Image stats
        lines.extend(
            [
                "",
                "-" * 100,
                "  OTA IMAGE STATISTICS",
                "-" * 100,
                f"  {'Total Regulars':30s}: {self.total_regulars_num:>15,}",
                f"  {'Total Directories':30s}: {self.total_directories_num:>15,}",
                f"  {'Total Symlinks':30s}: {self.total_symlinks_num:>15,}",
                f"  {'Total Size':30s}: {self.total_files_size:>15,} bytes ({self.total_files_size / (1024*1024):,.2f} MB)",
                "",
                f"  {'Delta Download Files':30s}: {self.delta_download_files_num:>15,}",
                f"  {'Delta Download Size':30s}: {self.delta_download_files_size:>15,} bytes ({self.delta_download_files_size / (1024*1024):,.2f} MB)",
                f"  {'Actually Downloaded':30s}: {self.downloaded_bytes:>15,} bytes ({self.downloaded_bytes / (1024*1024):,.2f} MB)",
                f"  {'Download Errors':30s}: {self.downloaded_errors:>15,}",
            ]
        )

        # Delta efficiency
        if self.total_files_size > 0:
            saved = self.total_files_size - self.downloaded_bytes
            efficiency = (saved / self.total_files_size) * 100
            lines.append(
                f"  {'Delta Efficiency':30s}: {efficiency:>14.2f}% ({saved / (1024*1024):,.2f} MB saved)"
            )

        # Boot controller timings
        lines.extend(
            [
                "",
                "-" * 100,
                "  BOOT CONTROLLER TIMINGS (Mock)",
                "-" * 100,
                f"  {'pre_update':30s}: {self.boot_timings.pre_update_duration_ms:>15,.3f} ms",
                f"  {'post_update':30s}: {self.boot_timings.post_update_duration_ms:>15,.3f} ms",
                f"  {'finalizing_update':30s}: {self.boot_timings.finalizing_update_duration_ms:>15,.3f} ms",
                f"  {'Reboot Triggered':30s}: {str(self.boot_timings.reboot_triggered):>15s}",
            ]
        )

        # Status transitions
        if self.status_transitions:
            lines.extend(
                [
                    "",
                    "-" * 100,
                    "  STATUS TRANSITIONS",
                    "-" * 100,
                ]
            )
            for ts_ns, status in self.status_transitions:
                relative_ms = (ts_ns - self.test_start_ns) / 1_000_000
                lines.append(f"  +{relative_ms:>12,.3f} ms: {status}")

        lines.extend(["", "=" * 100, ""])
        return "\n".join(lines)

    def to_json(self) -> str:
        """Export as JSON for analysis."""
        data = {
            "test_name": self.test_name,
            "ota_image_format": self.ota_image_format,
            "timestamp": self.timestamp,
            "total_duration_ms": self.total_duration_ms,
            "total_duration_s": self.total_duration_s,
            "phases": {
                name: {
                    "duration_ms": p.duration_ms,
                    "duration_s": p.duration_s,
                    "files_processed": p.files_processed,
                    "bytes_processed": p.bytes_processed,
                    "throughput_mb_s": p.throughput_mb_s,
                    "files_per_s": p.files_per_s,
                    "errors": p.errors,
                    "extra": p.extra,
                }
                for name, p in self.phases.items()
            },
            "ota_image_stats": {
                "total_regulars_num": self.total_regulars_num,
                "total_directories_num": self.total_directories_num,
                "total_symlinks_num": self.total_symlinks_num,
                "total_files_size_bytes": self.total_files_size,
                "delta_download_files_num": self.delta_download_files_num,
                "delta_download_files_size_bytes": self.delta_download_files_size,
                "downloaded_bytes": self.downloaded_bytes,
                "downloaded_errors": self.downloaded_errors,
            },
            "boot_controller_timings_ms": {
                "pre_update": self.boot_timings.pre_update_duration_ms,
                "post_update": self.boot_timings.post_update_duration_ms,
                "finalizing_update": self.boot_timings.finalizing_update_duration_ms,
                "reboot_triggered": self.boot_timings.reboot_triggered,
            },
        }
        return json.dumps(data, indent=2)


@dataclass
class ComparisonReport:
    """Generate comparison report between Legacy and V1 OTA image formats."""

    legacy_report: PerformanceReport | None = None
    v1_report: PerformanceReport | None = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def generate_markdown_table(self) -> str:
        """Generate GitHub PR comment compatible markdown table."""
        if not self.legacy_report or not self.v1_report:
            return "## Performance Comparison Report\n\n⚠️ Missing test results"

        legacy = self.legacy_report
        v1 = self.v1_report

        lines = [
            "## 📊 OTA Update Performance Comparison Report",
            "",
            f"> Generated: {self.timestamp}",
            "",
            "### ⏱️ Execution Time",
            "",
            "| Phase | Legacy (ms) | V1 (ms) |",
            "|:------|------------:|--------:|",
        ]

        # Total duration
        lines.append(
            f"| **Total Duration** | {legacy.total_duration_ms:,.1f} | {v1.total_duration_ms:,.1f} |"
        )

        # Phase comparison
        phase_order = [
            ("updater_init", "Updater Init"),
            ("execute_total", "Execute Total"),
            ("metadata_processing", "Metadata Processing"),
            ("delta_calculation", "Delta Calculation"),
            ("download", "Download"),
            ("apply_update", "Apply Update"),
            ("post_update", "Post Update"),
            ("finalization", "Finalization"),
        ]

        for phase_key, phase_name in phase_order:
            legacy_phase = legacy.phases.get(phase_key)
            v1_phase = v1.phases.get(phase_key)

            if legacy_phase and v1_phase:
                lines.append(
                    f"| {phase_name} | {legacy_phase.duration_ms:,.1f} | {v1_phase.duration_ms:,.1f} |"
                )
            elif legacy_phase:
                lines.append(f"| {phase_name} | {legacy_phase.duration_ms:,.1f} | - |")
            elif v1_phase:
                lines.append(f"| {phase_name} | - | {v1_phase.duration_ms:,.1f} |")

        # Boot controller timings
        lines.extend(
            [
                "",
                "### 🔧 Boot Controller Timings",
                "",
                "| Operation | Legacy (ms) | V1 (ms) |",
                "|:----------|------------:|--------:|",
            ]
        )

        boot_ops = [
            (
                "pre_update",
                "Pre Update",
                legacy.boot_timings.pre_update_duration_ms,
                v1.boot_timings.pre_update_duration_ms,
            ),
            (
                "post_update",
                "Post Update",
                legacy.boot_timings.post_update_duration_ms,
                v1.boot_timings.post_update_duration_ms,
            ),
            (
                "finalizing_update",
                "Finalizing Update",
                legacy.boot_timings.finalizing_update_duration_ms,
                v1.boot_timings.finalizing_update_duration_ms,
            ),
        ]

        for _, name, legacy_val, v1_val in boot_ops:
            lines.append(f"| {name} | {legacy_val:,.3f} | {v1_val:,.3f} |")

        # OTA Image stats
        lines.extend(
            [
                "",
                "### 📦 OTA Image Statistics",
                "",
                "| Metric | Legacy | V1 |",
                "|:-------|-------:|---:|",
            ]
        )

        stats = [
            ("Total Regular Files", legacy.total_regulars_num, v1.total_regulars_num),
            (
                "Total Directories",
                legacy.total_directories_num,
                v1.total_directories_num,
            ),
            ("Total Symlinks", legacy.total_symlinks_num, v1.total_symlinks_num),
            (
                "Total Size (MB)",
                legacy.total_files_size / (1024 * 1024),
                v1.total_files_size / (1024 * 1024),
            ),
            (
                "Delta Download Files",
                legacy.delta_download_files_num,
                v1.delta_download_files_num,
            ),
            (
                "Delta Download Size (MB)",
                legacy.delta_download_files_size / (1024 * 1024),
                v1.delta_download_files_size / (1024 * 1024),
            ),
            (
                "Actually Downloaded (MB)",
                legacy.downloaded_bytes / (1024 * 1024),
                v1.downloaded_bytes / (1024 * 1024),
            ),
        ]

        for name, legacy_val, v1_val in stats:
            if isinstance(legacy_val, int):
                lines.append(f"| {name} | {legacy_val:,} | {v1_val:,} |")
            else:
                lines.append(f"| {name} | {legacy_val:,.2f} | {v1_val:,.2f} |")

        # Delta efficiency
        legacy_eff = (
            (
                (legacy.total_files_size - legacy.downloaded_bytes)
                / legacy.total_files_size
                * 100
            )
            if legacy.total_files_size > 0
            else 0
        )
        v1_eff = (
            ((v1.total_files_size - v1.downloaded_bytes) / v1.total_files_size * 100)
            if v1.total_files_size > 0
            else 0
        )
        lines.append(f"| **Delta Efficiency (%)** | {legacy_eff:.2f} | {v1_eff:.2f} |")

        # Throughput section
        lines.extend(
            [
                "",
                "### 🚀 Throughput",
                "",
                "| Metric | Legacy | V1 |",
                "|:-------|-------:|---:|",
            ]
        )

        # Get execute_total phase for throughput
        legacy_exec = legacy.phases.get("execute_total")
        v1_exec = v1.phases.get("execute_total")

        if legacy_exec and v1_exec:
            lines.append(
                f"| Files/second | {legacy_exec.files_per_s:,.1f} | {v1_exec.files_per_s:,.1f} |"
            )
            lines.append(
                f"| MB/second | {legacy_exec.throughput_mb_s:.2f} | {v1_exec.throughput_mb_s:.2f} |"
            )

        # Download phase throughput
        legacy_dl = legacy.phases.get("download")
        v1_dl = v1.phases.get("download")

        if legacy_dl and v1_dl:
            lines.append(
                f"| Download Files/second | {legacy_dl.files_per_s:,.1f} | {v1_dl.files_per_s:,.1f} |"
            )
            lines.append(
                f"| Download MB/second | {legacy_dl.throughput_mb_s:.2f} | {v1_dl.throughput_mb_s:.2f} |"
            )

        lines.append("")

        return "\n".join(lines)

    def save_to_file(self, output_path: Path) -> None:
        """Save markdown report to file."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(self.generate_markdown_table())
        logger.info(f"Comparison report saved to: {output_path}")


# Global storage for comparison report (used across test session)
_comparison_reports: dict[str, PerformanceReport] = {}


def store_report_for_comparison(report: PerformanceReport) -> None:
    """Store a report for later comparison."""
    _comparison_reports[report.ota_image_format] = report


def get_comparison_report() -> ComparisonReport:
    """Get the comparison report with all stored results."""
    return ComparisonReport(
        legacy_report=_comparison_reports.get("legacy"),
        v1_report=_comparison_reports.get("v1"),
    )


def clear_comparison_reports() -> None:
    """Clear stored reports."""
    _comparison_reports.clear()


class MockBootController:
    """Mock boot controller for performance E2E tests."""

    def __init__(
        self,
        standby_slot_path: Path,
        standby_slot_dev: Path,
        timings: MockBootControllerTimings,
        *,
        current_version: str = "123.x",
        standby_version: str = "",
        initial_ota_status: OTAStatus = OTAStatus.SUCCESS,
    ):
        self._standby_slot_path = standby_slot_path
        self._standby_slot_dev = standby_slot_dev
        self._timings = timings
        self._current_version = current_version
        self._standby_version = standby_version
        self._initial_ota_status = initial_ota_status
        self._bootloader_type = "mock_grub"

    def get_booted_ota_status(self) -> OTAStatus:
        return self._initial_ota_status

    def get_standby_slot_path(self) -> Path:
        return self._standby_slot_path

    @property
    def bootloader_type(self) -> str:
        return self._bootloader_type

    @property
    def standby_slot_dev(self) -> Path:
        return self._standby_slot_dev

    def get_standby_slot_dev(self) -> str:
        return str(self._standby_slot_dev)

    def load_version(self) -> str:
        return self._current_version

    def load_standby_slot_version(self) -> str:
        return self._standby_version

    def on_operation_failure(self) -> None:
        logger.info("MockBootController: on_operation_failure")

    def pre_update(self, *, standby_as_ref: bool, erase_standby: bool) -> None:
        self._timings.pre_update_start_ns = time.time_ns()
        logger.info(
            f"MockBootController: pre_update(standby_as_ref={standby_as_ref}, erase_standby={erase_standby})"
        )
        self._standby_slot_path.mkdir(parents=True, exist_ok=True)
        self._timings.pre_update_end_ns = time.time_ns()

    def post_update(self, update_version: str) -> None:
        self._timings.post_update_start_ns = time.time_ns()
        logger.info(f"MockBootController: post_update(version={update_version})")
        self._standby_version = update_version
        self._timings.post_update_end_ns = time.time_ns()

    def finalizing_update(self, *, chroot: str | None = None) -> None:
        self._timings.finalizing_update_start_ns = time.time_ns()
        logger.info(f"MockBootController: finalizing_update(chroot={chroot})")
        self._timings.finalizing_update_called = True
        self._timings.reboot_triggered = True
        self._timings.finalizing_update_end_ns = time.time_ns()
        raise MockRebootTriggered("Mock reboot triggered")


class MockRebootTriggered(Exception):
    """Exception raised when mock boot controller triggers reboot."""

    pass


@pytest.fixture
def performance_report() -> PerformanceReport:
    return PerformanceReport()


@pytest.fixture
def mock_boot_controller_timings() -> MockBootControllerTimings:
    return MockBootControllerTimings()


@pytest.fixture(scope="module", autouse=True)
def generate_comparison_report_at_end():
    """Generate comparison report after all performance tests complete."""
    import os

    # Clear any previous reports at the start
    clear_comparison_reports()

    yield

    # After all tests, generate the comparison report
    comparison = get_comparison_report()
    markdown_report = comparison.generate_markdown_table()

    # Print to stdout for CI visibility
    print("\n" + "=" * 100)
    print("  LEGACY vs V1 COMPARISON REPORT (for GitHub PR comment)")
    print("=" * 100)
    print(markdown_report)
    print("=" * 100)

    # Log it as well
    logger.info(f"\n{markdown_report}")

    # Save to file in test_result directory
    # Use OUTPUT_DIR env var if set (Docker), otherwise use relative path
    output_dir_env = os.environ.get("OUTPUT_DIR")
    if output_dir_env:
        output_dir = Path(output_dir_env)
    else:
        output_dir = Path(__file__).parent.parent.parent.parent / "test_result"
    output_path = output_dir / "performance_comparison.md"
    comparison.save_to_file(output_path)
    print(f"\n📁 Comparison report saved to: {output_path}")
