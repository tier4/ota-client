# Copyright 2026 TIER IV, INC. All rights reserved.
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
"""The image ID the running image carries is the one reported; otaclient's record
only adds its release."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from otaclient._types import VersionDetail
from otaclient.boot_control import _image_version_file
from otaclient.boot_control._image_version_file import (
    MAX_VERSION_FILE_SIZE,
    load_image_version_detail,
    resolve_active_slot_version_detail,
)
from otaclient.boot_control._ota_status_control import OTAStatusFilesControl
from otaclient_common import _env

IMAGE_ID = "0b5c2a4e-6f1d-4c3b-9a8e-2d7f1e0c9b6a"

# what Web.Auto writes, schema_version 1, in the shape of a real one (the IDs made up)
VERSION_FILE = {
    "artifact": "main",
    "build": {
        "architecture": "x86_64",
        "build_report_id": "6c1d0f3a-2b4e-5f6a-8b9c-0d1e2f3a4b5c",
        "built_at": "2026-11-06T12:05:32.819Z",
        "catalog_id": "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d",
        "clean_build": False,
        "has_developer_env_vars": False,
        "incremental_build": False,
        "kernel_version": "6.8.0-100-generic",
        "phases": {
            "autoware-build": {
                "incremental": False,
                "required": True,
                "result_reused": False,
            },
        },
        "project_id": "a_project",
        "spec_name": "custom-v2-std2-amd64-xl",
    },
    "image_id": IMAGE_ID,
    "ml_packages": [
        {
            "digest": "sha256:" + "3" * 64,
            "name": "a_model",
            "release_id": "7e6d5c4b-3a29-4180-9f7e-6d5c4b3a2918",
            "release_name": "a_model_v1.2.0",
        },
    ],
    "schema_version": 1,
    "source": {
        "commit": "1" * 40,
        "url": "https://github.com/tier4/pilot-auto.x2.git",
    },
    "source_revisions": {
        "home/autoware/pilot-auto": "1" * 40,
        "opt/acados": "2" * 40,
    },
}


def version_file_path(root: Path) -> Path:
    return root / "usr/share/ota/version.json"


def write_version_file(root: Path, content: str | bytes | dict) -> Path:
    """As Web.Auto writes it: `JSON.stringify(file, null, 2)` and a newline."""
    _fpath = version_file_path(root)
    _fpath.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, dict):
        content = json.dumps(content, indent=2) + "\n"
    if isinstance(content, str):
        content = content.encode()
    _fpath.write_bytes(content)
    return _fpath


def with_image_id(image_id: object) -> dict:
    return {**VERSION_FILE, "image_id": image_id}


class TestTheImagesVersionFile:
    def test_the_image_id_is_taken_and_nothing_else(self, tmp_path: Path):
        """The release is named after the build, so the image has none to give; the
        releases of the ML packages it carries are theirs, not the image's."""
        write_version_file(tmp_path, VERSION_FILE)

        assert load_image_version_detail(root=tmp_path) == VersionDetail(
            release_name="", release_id="", image_id=IMAGE_ID
        )

    def test_an_image_without_the_file_has_no_detail(self, tmp_path: Path):
        assert load_image_version_detail(root=tmp_path) is None

    @pytest.mark.parametrize(
        "content",
        (
            pytest.param("{not json", id="not_json"),
            pytest.param("[]", id="not_an_object"),
            pytest.param({"schema_version": 1}, id="no_image_id"),
            pytest.param(with_image_id(""), id="empty_image_id"),
            pytest.param(with_image_id(1), id="image_id_not_a_string"),
            pytest.param(with_image_id("a\nb"), id="image_id_with_a_newline"),
            pytest.param(with_image_id('a"b'), id="image_id_with_a_quote"),
            pytest.param(with_image_id("a" * 257), id="image_id_too_long"),
        ),
    )
    def test_a_file_that_names_no_valid_image_id_gives_no_detail(
        self, tmp_path: Path, content: str | dict
    ):
        """The image ID goes into the uploaded JSON logs and the status API as it is."""
        write_version_file(tmp_path, content)

        assert load_image_version_detail(root=tmp_path) is None

    @pytest.mark.parametrize(
        "schema_version",
        (
            pytest.param(2, id="a_later_one"),
            pytest.param(None, id="none"),
            pytest.param("1", id="not_a_number"),
        ),
    )
    def test_a_file_of_another_schema_version_gives_no_detail(
        self, tmp_path: Path, schema_version: object
    ):
        """What a later schema means by image_id is not this otaclient's to guess."""
        write_version_file(tmp_path, {**VERSION_FILE, "schema_version": schema_version})

        assert load_image_version_detail(root=tmp_path) is None

    def test_a_file_too_large_is_not_read(self, tmp_path: Path):
        _padding = "x" * MAX_VERSION_FILE_SIZE
        write_version_file(tmp_path, {**VERSION_FILE, "padding": _padding})

        assert load_image_version_detail(root=tmp_path) is None

    def test_a_symlink_is_not_followed(self, tmp_path: Path):
        """In an app image an absolute link would resolve in the app image's root."""
        _real = write_version_file(tmp_path / "elsewhere", VERSION_FILE)
        _link = version_file_path(tmp_path)
        _link.parent.mkdir(parents=True)
        _link.symlink_to(_real)

        assert load_image_version_detail(root=tmp_path) is None

    def test_what_is_not_a_regular_file_is_not_read(self, tmp_path: Path):
        version_file_path(tmp_path).mkdir(parents=True)

        assert load_image_version_detail(root=tmp_path) is None

    def test_a_file_that_cannot_be_read_is_not_logged_whole(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """A UnicodeDecodeError's repr carries every byte it was given."""
        write_version_file(tmp_path, b'{"project_id": "not-for-the-log", "x": "\xff"}')

        with caplog.at_level(logging.WARNING, logger=_image_version_file.__name__):
            assert load_image_version_detail(root=tmp_path) is None
        assert "UnicodeDecodeError" in caplog.text
        assert "not-for-the-log" not in caplog.text

    def test_an_app_image_reads_the_hosts_file(
        self, tmp_path: Path, mocker: MockerFixture
    ):
        """An app image's own /usr is not the host's: the file is read through the
        host's root."""
        write_version_file(tmp_path, VERSION_FILE)
        mocker.patch.object(
            _env, "get_dynamic_client_chroot_path", return_value=str(tmp_path)
        )

        _detail = load_image_version_detail()
        assert _detail is not None and _detail.image_id == IMAGE_ID

    def test_otaclient_on_the_host_reads_the_file_from_the_root(
        self, tmp_path: Path, mocker: MockerFixture
    ):
        """Not an app image: the file is read where the constant says, from /."""
        _fpath = write_version_file(tmp_path, VERSION_FILE)
        mocker.patch.object(_env, "get_dynamic_client_chroot_path", return_value=None)
        mocker.patch.object(
            _image_version_file.cfg, "IMAGE_VERSION_FILE_FPATH", str(_fpath)
        )

        _detail = load_image_version_detail()
        assert _detail is not None and _detail.image_id == IMAGE_ID


class TestTheRunningSlotsDetail:
    """The image ID is the running image's; the release is the record's, while the
    record is about that image."""

    @pytest.fixture
    def status_control(self, mocker: MockerFixture):
        _ctrl = mocker.MagicMock(spec=OTAStatusFilesControl)
        _ctrl.load_active_slot_version_detail.return_value = None
        return _ctrl

    @pytest.fixture
    def from_image(self, mocker: MockerFixture):
        return mocker.patch.object(
            _image_version_file,
            "load_image_version_detail",
            return_value=VersionDetail(image_id=IMAGE_ID),
        )

    def test_a_record_of_the_running_image_keeps_its_release(
        self, status_control, from_image
    ):
        _recorded = VersionDetail(
            release_name="v4.5.3", release_id="rid", image_id=IMAGE_ID
        )
        status_control.load_active_slot_version_detail.return_value = _recorded

        assert resolve_active_slot_version_detail(status_control) == _recorded

    def test_a_record_of_another_image_loses_its_release(
        self, status_control, from_image
    ):
        """The slot no longer runs what otaclient installed into it: reporting that
        release would claim a release the device does not run."""
        status_control.load_active_slot_version_detail.return_value = VersionDetail(
            release_name="v4.5.3", release_id="rid", image_id="another-image"
        )

        assert resolve_active_slot_version_detail(status_control) == VersionDetail(
            image_id=IMAGE_ID
        )

    def test_a_slot_with_no_detail_recorded_reports_the_images_id(
        self, status_control, from_image
    ):
        """A device the factory installer wrote, or one updated by a request that
        named no release (the version alone recorded; with real files in
        test_partition_image)."""
        assert resolve_active_slot_version_detail(status_control) == VersionDetail(
            image_id=IMAGE_ID
        )

    def test_an_image_without_the_file_reports_the_record(
        self, status_control, from_image
    ):
        """An image built before the file was written, or with one that is not read:
        the record is all there is."""
        from_image.return_value = None
        _recorded = VersionDetail(
            release_name="v4.5.3", release_id="rid", image_id="the-requested-image"
        )
        status_control.load_active_slot_version_detail.return_value = _recorded

        assert resolve_active_slot_version_detail(status_control) == _recorded

    def test_nothing_anywhere_is_none(self, status_control, from_image):
        from_image.return_value = None

        assert resolve_active_slot_version_detail(status_control) is None
