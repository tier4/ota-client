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
from types import SimpleNamespace

import pytest
from pytest_mock import MockerFixture

from otaclient.boot_control import _grub_new
from otaclient.boot_control._grub_new import GrubBootController
from otaclient.boot_control._jetson_common import preserve_ota_config_files_to_standby
from otaclient.boot_control._ota_config_files import (
    ECU_INFO_FNAME,
    PROXY_INFO_FNAME,
    install_ota_config_files,
    load_ota_config_files_from_image,
)
from otaclient.boot_control._slot_mnt_helper import SlotMountHelper


def _ecu_info(ecu_id: str, ip_addr: str) -> str:
    return (
        "format_version: 1\n"
        f'ecu_id: "{ecu_id}"\n'
        f'ip_addr: "{ip_addr}"\n'
        'bootloader: "grub"\n'
        "secondaries:\n"
        '  - ecu_id: "sub"\n'
        f'    ip_addr: "{ip_addr[:-1]}2"\n'
    )


OLD_ECU_INFO = _ecu_info("autoware", "192.168.30.1")
NEW_ECU_INFO = _ecu_info("autoware", "192.168.20.1")
OTHER_ECU_INFO = _ecu_info("perception", "192.168.20.1")
OLD_PROXY_INFO = 'upper_ota_proxy: "http://192.168.30.1:8082"\n'
NEW_PROXY_INFO = 'upper_ota_proxy: "http://192.168.20.1:8082"\n'


def _write_ota_dir(_dir: Path, files: dict[str, str]) -> Path:
    _dir.mkdir(parents=True, exist_ok=True)
    for _fname, _content in files.items():
        (_dir / _fname).write_text(_content)
    return _dir


def _read_ota_dir(_dir: Path) -> dict[str, str]:
    return {_f.name: _f.read_text() for _f in _dir.iterdir()}


@pytest.mark.parametrize(
    "image_files",
    (
        {ECU_INFO_FNAME: NEW_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
        {PROXY_INFO_FNAME: NEW_PROXY_INFO},
        {},
    ),
)
def test_load_ota_config_files_from_image(tmp_path: Path, image_files: dict):
    _image_ota_dir = _write_ota_dir(
        tmp_path / "boot/ota", {**image_files, "other_file": "other"}
    )
    assert load_ota_config_files_from_image(_image_ota_dir) == image_files


def test_load_ota_config_files_from_image_no_ota_dir(tmp_path: Path):
    assert load_ota_config_files_from_image(tmp_path / "boot/ota") == {}


@pytest.mark.parametrize(
    "image_files, current_files, expected",
    (
        # the ecu_info.yaml with the same ecu_id from the image is installed
        (
            {ECU_INFO_FNAME: NEW_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
            {ECU_INFO_FNAME: OLD_ECU_INFO, PROXY_INFO_FNAME: OLD_PROXY_INFO},
            {ECU_INFO_FNAME: NEW_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
        ),
        # ecu_id mismatched, keep the current ecu_info.yaml
        (
            {ECU_INFO_FNAME: OTHER_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
            {ECU_INFO_FNAME: OLD_ECU_INFO, PROXY_INFO_FNAME: OLD_PROXY_INFO},
            {ECU_INFO_FNAME: OLD_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
        ),
        # invalid ecu_info.yaml from the image, keep the current ecu_info.yaml
        (
            {ECU_INFO_FNAME: "ip_addr: 192.168.20.1\n"},
            {ECU_INFO_FNAME: OLD_ECU_INFO},
            {ECU_INFO_FNAME: OLD_ECU_INFO},
        ),
        (
            {ECU_INFO_FNAME: "not a yaml mapping"},
            {ECU_INFO_FNAME: OLD_ECU_INFO},
            {ECU_INFO_FNAME: OLD_ECU_INFO},
        ),
        # no ecu_info.yaml currently, install the one from the image
        (
            {ECU_INFO_FNAME: OTHER_ECU_INFO},
            {},
            {ECU_INFO_FNAME: OTHER_ECU_INFO},
        ),
        # the current ecu_info.yaml is invalid, install the one from the image
        (
            {ECU_INFO_FNAME: OTHER_ECU_INFO},
            {ECU_INFO_FNAME: "not a yaml mapping"},
            {ECU_INFO_FNAME: OTHER_ECU_INFO},
        ),
        # files not shipped in the image are left as they are
        (
            {},
            {ECU_INFO_FNAME: OLD_ECU_INFO, PROXY_INFO_FNAME: OLD_PROXY_INFO},
            {ECU_INFO_FNAME: OLD_ECU_INFO, PROXY_INFO_FNAME: OLD_PROXY_INFO},
        ),
    ),
)
def test_install_ota_config_files(
    tmp_path: Path, image_files: dict, current_files: dict, expected: dict
):
    _ota_dir = _write_ota_dir(tmp_path / "boot/ota", current_files)

    install_ota_config_files(
        image_files,
        dst_ota_dpath=_ota_dir,
        current_ecu_info_fpath=_ota_dir / ECU_INFO_FNAME,
    )

    assert _read_ota_dir(_ota_dir) == expected


def test_install_ota_config_files_no_dst_dir(tmp_path: Path):
    _ota_dir = tmp_path / "boot/ota"

    install_ota_config_files(
        {ECU_INFO_FNAME: NEW_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
        dst_ota_dpath=_ota_dir,
        current_ecu_info_fpath=_ota_dir / ECU_INFO_FNAME,
    )

    assert _read_ota_dir(_ota_dir) == {
        ECU_INFO_FNAME: NEW_ECU_INFO,
        PROXY_INFO_FNAME: NEW_PROXY_INFO,
    }


def test_grub_install_new_ota_config_files(tmp_path: Path, mocker: MockerFixture):
    """The grub boot controller installs the files from the OTA image to the shared /boot/ota."""
    _shared_ota_dir = _write_ota_dir(
        tmp_path / "boot/ota",
        {ECU_INFO_FNAME: OLD_ECU_INFO, PROXY_INFO_FNAME: OLD_PROXY_INFO},
    )
    _standby_mp = tmp_path / "standby"
    _write_ota_dir(
        _standby_mp / _shared_ota_dir.relative_to("/"),
        {ECU_INFO_FNAME: NEW_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
    )
    mocker.patch.object(
        _grub_new,
        "cfg",
        SimpleNamespace(
            CANONICAL_ROOT="/",
            OTA_DPATH=str(_shared_ota_dir),
            ECU_INFO_FPATH=str(_shared_ota_dir / ECU_INFO_FNAME),
        ),
    )
    _controller = object.__new__(GrubBootController)
    _controller._mp_control = SimpleNamespace(standby_slot_mount_point=_standby_mp)  # type: ignore[assignment]

    _controller._install_new_ota_config_files()

    assert _read_ota_dir(_shared_ota_dir) == {
        ECU_INFO_FNAME: NEW_ECU_INFO,
        PROXY_INFO_FNAME: NEW_PROXY_INFO,
    }


class TestPreserveOTAFolderToStandby:
    """Both the Jetson and the RPi boot controllers preserve the active slot's /boot/ota,
    with the OTA config files from the OTA image installed over it."""

    @pytest.fixture(params=["jetson", "rpi"])
    def preserve_func(self, request):
        def _jetson(active_ota_dir: Path, standby_ota_dir: Path):
            preserve_ota_config_files_to_standby(
                active_slot_ota_dirpath=active_ota_dir,
                standby_slot_ota_dirpath=standby_ota_dir,
            )

        def _rpi(active_ota_dir: Path, standby_ota_dir: Path):
            _mp_control = SimpleNamespace(
                active_slot_mount_point=active_ota_dir.parents[1],
                standby_slot_mount_point=standby_ota_dir.parents[1],
            )
            SlotMountHelper.preserve_ota_folder_to_standby(_mp_control)  # type: ignore[arg-type]

        return _jetson if request.param == "jetson" else _rpi

    @pytest.mark.parametrize(
        "image_files, expected_config_files",
        (
            (
                {ECU_INFO_FNAME: NEW_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
                {ECU_INFO_FNAME: NEW_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
            ),
            (
                {ECU_INFO_FNAME: OTHER_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
                {ECU_INFO_FNAME: OLD_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
            ),
            (
                {},
                {ECU_INFO_FNAME: OLD_ECU_INFO, PROXY_INFO_FNAME: OLD_PROXY_INFO},
            ),
        ),
    )
    def test_preserve(
        self,
        tmp_path: Path,
        preserve_func,
        image_files: dict,
        expected_config_files: dict,
    ):
        _active_ota_dir = _write_ota_dir(
            tmp_path / "active/boot/ota",
            {
                ECU_INFO_FNAME: OLD_ECU_INFO,
                PROXY_INFO_FNAME: OLD_PROXY_INFO,
                "active_only_file": "active",
            },
        )
        _standby_ota_dir = _write_ota_dir(tmp_path / "standby/boot/ota", image_files)

        preserve_func(_active_ota_dir, _standby_ota_dir)

        _res = _read_ota_dir(_standby_ota_dir)
        assert _res.pop("active_only_file") == "active"
        assert _res == expected_config_files
        # the active slot's /boot/ota is not touched
        assert _read_ota_dir(_active_ota_dir) == {
            ECU_INFO_FNAME: OLD_ECU_INFO,
            PROXY_INFO_FNAME: OLD_PROXY_INFO,
            "active_only_file": "active",
        }
