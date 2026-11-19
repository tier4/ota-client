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

from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import pytest
import pytest_mock

from ota_metadata.legacy2.metadata import OTAMetadata
from otaclient._status_monitor import StatusReport
from otaclient._types import AbortState, OTAStatus
from otaclient.boot_control._ota_config_files import OTAConfigFiles
from otaclient.ota_core import _updater
from otaclient.ota_core._abort_handler import WAIT_FOR_STATUS_REPORT, AbortHandler


class TestAbortHandlerStatusWait:
    """Test that AbortHandler waits for status report propagation."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.status_report_queue: Queue[StatusReport] = Queue()

    def _make_handler(self, mocker) -> AbortHandler:
        ota_client = mocker.MagicMock()
        ota_client.live_ota_status = OTAStatus.UPDATING
        ota_client.report_status.side_effect = self.status_report_queue.put_nowait
        handler = AbortHandler(
            ota_client=ota_client,
        )
        handler.set_session(session_id="test_session")
        return handler

    def test_perform_abort_waits_for_status_report(self, mocker):
        """Test that _perform_abort sleeps to allow status report propagation."""
        handler = self._make_handler(mocker)
        mocker.patch("otaclient.ota_core._abort_handler.os.kill")
        mock_sleep = mocker.patch("otaclient.ota_core._abort_handler.time.sleep")

        handler._state = AbortState.ABORTING
        handler._perform_abort()

        mock_sleep.assert_called_once_with(WAIT_FOR_STATUS_REPORT)


class TestPostUpdateOTAConfigFiles:
    """The OTA config files are loaded from the OTA image before the persist files handling."""

    NEW_ECU_INFO = 'format_version: 1\necu_id: "autoware"\nip_addr: "192.168.20.1"\n'
    NEW_PROXY_INFO = 'upper_ota_proxy: "http://192.168.20.1:8082"\n'

    @pytest.fixture
    def updater(self, tmp_path: Path, mocker: pytest_mock.MockerFixture):
        self.standby_ota_dir = tmp_path / "standby/boot/ota"
        self.standby_ota_dir.mkdir(parents=True)
        (self.standby_ota_dir / "ecu_info.yaml").write_text(self.NEW_ECU_INFO)
        (self.standby_ota_dir / "proxy_info.yaml").write_text(self.NEW_PROXY_INFO)

        def _persist(_entries, *, active_slot_mp, standby_slot_mp):
            # simulate the persist files handling copying the active slot's files over
            for _f in self.standby_ota_dir.iterdir():
                _f.write_text("from the active slot")

        self.process_persistents = mocker.patch(
            f"{_updater.__name__}.process_persistents", side_effect=_persist
        )
        self.ecu_info = SimpleNamespace(ecu_id="autoware")
        mocker.patch(f"{_updater.__name__}.ecu_info", self.ecu_info)
        _resource_dir = tmp_path / "resource_dir"
        _resource_dir.mkdir()
        return SimpleNamespace(
            _status_report_queue=Queue(),
            session_id="test_session",
            _metrics=SimpleNamespace(),
            _active_slot_mp=tmp_path / "active",
            _standby_slot_mp=tmp_path / "standby",
            _resource_dir_on_standby=_resource_dir,
            _ota_meta_store_on_standby=_resource_dir,
            _preserve_ota_image_meta_at_post_update=mocker.MagicMock(),
            _preserve_client_squashfs_at_post_update=mocker.MagicMock(),
            release_name="",
            release_id="",
            image_id="",
            update_version="2.0.0",
            _boot_controller=mocker.MagicMock(),
        )

    @pytest.mark.parametrize(
        "persist_entries, expected",
        (
            (
                None,
                OTAConfigFiles(ecu_info=NEW_ECU_INFO, proxy_info=NEW_PROXY_INFO),
            ),
            (
                ["/etc/hosts"],
                OTAConfigFiles(ecu_info=NEW_ECU_INFO, proxy_info=NEW_PROXY_INFO),
            ),
            # persisted files are not loaded, the current ones are kept
            (
                ["/boot/ota/ecu_info.yaml"],
                OTAConfigFiles(proxy_info=NEW_PROXY_INFO),
            ),
            (["/boot/ota"], OTAConfigFiles()),
        ),
    )
    def test_post_update(self, updater, persist_entries, expected):
        updater._iter_persists_func = lambda: (
            iter(persist_entries) if persist_entries is not None else None
        )

        _updater.OTAUpdaterBase._post_update(updater)  # type: ignore[arg-type]

        if persist_entries is None:
            self.process_persistents.assert_not_called()
        else:
            self.process_persistents.assert_called_once()
            assert list(self.process_persistents.call_args.args[0]) == persist_entries
        updater._boot_controller.post_update.assert_called_once_with(
            "2.0.0", version_detail=None, ota_config_files=expected
        )

    def test_post_update_ecu_id_mismatched(self, updater):
        """The OTA image's ecu_info.yaml is compared with the running otaclient's ecu_id."""
        updater._iter_persists_func = lambda: None
        self.ecu_info.ecu_id = "perception1"

        _updater.OTAUpdaterBase._post_update(updater)  # type: ignore[arg-type]

        updater._boot_controller.post_update.assert_called_once_with(
            "2.0.0", version_detail=None, ota_config_files=OTAConfigFiles()
        )

    def test_post_update_legacy_persists_txt(self, updater, tmp_path: Path):
        """The quoted entries of the legacy persists.txt are matched."""
        _session_dir = tmp_path / "session"
        _session_dir.mkdir()
        (_session_dir / OTAMetadata.PERSIST_META_FNAME).write_text(
            "'/boot/ota/ecu_info.yaml'\n'/etc/hosts'\n"
        )
        _legacy_metadata = object.__new__(OTAMetadata)
        _legacy_metadata._session_dir = _session_dir
        updater._iter_persists_func = _legacy_metadata.iter_persist_entries

        _updater.OTAUpdaterBase._post_update(updater)  # type: ignore[arg-type]

        assert list(self.process_persistents.call_args.args[0]) == [
            "/boot/ota/ecu_info.yaml",
            "/etc/hosts",
        ]
        updater._boot_controller.post_update.assert_called_once_with(
            "2.0.0",
            version_detail=None,
            ota_config_files=OTAConfigFiles(proxy_info=self.NEW_PROXY_INFO),
        )
