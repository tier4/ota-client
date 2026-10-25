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

from otaclient.boot_control import _jetson_cboot, _jetson_uefi
from otaclient.boot_control._jetson_cboot import JetsonCBootControl
from otaclient.boot_control._jetson_uefi import JetsonUEFIBootControl
from otaclient.boot_control._ota_config_files import (
    ECU_INFO_FNAME,
    PROXY_INFO_FNAME,
    OTAConfigFiles,
    backup_ota_config_files,
    install_ota_config_files,
    load_ota_config_files_from_image,
    preserve_ota_dir_and_install_config_files,
    restore_ota_config_files,
)


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


CURRENT_ECU_ID = "autoware"
OLD_ECU_INFO = _ecu_info(CURRENT_ECU_ID, "192.168.30.1")
NEW_ECU_INFO = _ecu_info(CURRENT_ECU_ID, "192.168.20.1")
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


def _load(image_ota_dir: Path, persist_entries=()) -> OTAConfigFiles:
    return load_ota_config_files_from_image(
        image_ota_dir, persist_entries=persist_entries, current_ecu_id=CURRENT_ECU_ID
    )


class TestLoadOTAConfigFilesFromImage:
    @pytest.mark.parametrize(
        "image_files, expected",
        (
            (
                {ECU_INFO_FNAME: NEW_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
                OTAConfigFiles(ecu_info=NEW_ECU_INFO, proxy_info=NEW_PROXY_INFO),
            ),
            (
                {PROXY_INFO_FNAME: NEW_PROXY_INFO},
                OTAConfigFiles(proxy_info=NEW_PROXY_INFO),
            ),
            ({}, OTAConfigFiles()),
            # ecu_id mismatched, the OTA image is not for this ECU
            (
                {ECU_INFO_FNAME: OTHER_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
                OTAConfigFiles(),
            ),
            # invalid ecu_info.yaml, only proxy_info.yaml is loaded
            (
                {
                    ECU_INFO_FNAME: "ip_addr: 192.168.20.1\n",
                    PROXY_INFO_FNAME: NEW_PROXY_INFO,
                },
                OTAConfigFiles(proxy_info=NEW_PROXY_INFO),
            ),
            (
                {
                    ECU_INFO_FNAME: "not a yaml mapping",
                    PROXY_INFO_FNAME: NEW_PROXY_INFO,
                },
                OTAConfigFiles(proxy_info=NEW_PROXY_INFO),
            ),
            # invalid proxy_info.yaml, only ecu_info.yaml is loaded
            (
                {ECU_INFO_FNAME: NEW_ECU_INFO, PROXY_INFO_FNAME: "not a yaml mapping"},
                OTAConfigFiles(ecu_info=NEW_ECU_INFO),
            ),
            (
                {
                    ECU_INFO_FNAME: NEW_ECU_INFO,
                    PROXY_INFO_FNAME: 'upper_ota_proxy: "192.168.20.1:8082"\n',
                },
                OTAConfigFiles(ecu_info=NEW_ECU_INFO),
            ),
        ),
    )
    def test_load(self, tmp_path: Path, image_files: dict, expected: OTAConfigFiles):
        _image_ota_dir = _write_ota_dir(
            tmp_path / "boot/ota", {**image_files, "other_file": "other"}
        )
        assert _load(_image_ota_dir) == expected

    def test_no_ota_dir(self, tmp_path: Path):
        assert _load(tmp_path / "boot/ota") == OTAConfigFiles()

    @pytest.mark.parametrize(
        "persist_entries, expected",
        (
            (["/boot/ota/ecu_info.yaml"], OTAConfigFiles(proxy_info=NEW_PROXY_INFO)),
            (["/boot/ota/proxy_info.yaml"], OTAConfigFiles(ecu_info=NEW_ECU_INFO)),
            (
                ["/boot/ota/ecu_info.yaml", "/boot/ota/proxy_info.yaml"],
                OTAConfigFiles(),
            ),
            # listed by a parent folder
            (["/boot/ota"], OTAConfigFiles()),
            (["/boot/ota/"], OTAConfigFiles()),
            (["/boot"], OTAConfigFiles()),
            # not listed
            (
                ["/etc/hosts", "/boot/ota/ecu_info.yaml.bak", "/boot/ota_backup"],
                OTAConfigFiles(ecu_info=NEW_ECU_INFO, proxy_info=NEW_PROXY_INFO),
            ),
        ),
    )
    def test_persisted(
        self, tmp_path: Path, persist_entries: list[str], expected: OTAConfigFiles
    ):
        """A file listed in the persist files is not loaded, so the current one is kept."""
        _image_ota_dir = _write_ota_dir(
            tmp_path / "boot/ota",
            {ECU_INFO_FNAME: NEW_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
        )
        assert _load(_image_ota_dir, persist_entries) == expected

    def test_persisted_ecu_info_skips_ecu_id_check(self, tmp_path: Path):
        """The ecu_id check is only done with the loaded ecu_info.yaml."""
        _image_ota_dir = _write_ota_dir(
            tmp_path / "boot/ota",
            {ECU_INFO_FNAME: OTHER_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
        )
        assert _load(_image_ota_dir, ["/boot/ota/ecu_info.yaml"]) == OTAConfigFiles(
            proxy_info=NEW_PROXY_INFO
        )

    def test_symlink(self, tmp_path: Path):
        """Symlinks in the OTA image are not followed, they would resolve against the running rootfs."""
        _target = tmp_path / "target.yaml"
        _target.write_text(NEW_ECU_INFO)
        _image_ota_dir = _write_ota_dir(
            tmp_path / "image/boot/ota", {PROXY_INFO_FNAME: NEW_PROXY_INFO}
        )
        (_image_ota_dir / ECU_INFO_FNAME).symlink_to(_target)

        assert _load(_image_ota_dir) == OTAConfigFiles(proxy_info=NEW_PROXY_INFO)

    def test_symlink_ota_dir(self, tmp_path: Path):
        _real_ota_dir = _write_ota_dir(
            tmp_path / "real_ota",
            {ECU_INFO_FNAME: NEW_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
        )
        _image_ota_dir = tmp_path / "image/boot/ota"
        _image_ota_dir.parent.mkdir(parents=True)
        _image_ota_dir.symlink_to(_real_ota_dir)

        assert _load(_image_ota_dir) == OTAConfigFiles()

    def test_not_utf8(self, tmp_path: Path):
        """A file that is not UTF-8 is not loaded, instead of failing the OTA."""
        _image_ota_dir = _write_ota_dir(
            tmp_path / "boot/ota", {PROXY_INFO_FNAME: NEW_PROXY_INFO}
        )
        (_image_ota_dir / ECU_INFO_FNAME).write_bytes(
            NEW_ECU_INFO.encode() + "# テスト\n".encode("shift_jis")
        )

        assert _load(_image_ota_dir) == OTAConfigFiles(proxy_info=NEW_PROXY_INFO)


class TestInstallOTAConfigFiles:
    @pytest.mark.parametrize(
        "ota_config_files, expected",
        (
            (
                OTAConfigFiles(ecu_info=NEW_ECU_INFO, proxy_info=NEW_PROXY_INFO),
                {ECU_INFO_FNAME: NEW_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
            ),
            (
                OTAConfigFiles(proxy_info=NEW_PROXY_INFO),
                {ECU_INFO_FNAME: OLD_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
            ),
            (
                OTAConfigFiles(),
                {ECU_INFO_FNAME: OLD_ECU_INFO, PROXY_INFO_FNAME: OLD_PROXY_INFO},
            ),
        ),
    )
    def test_install(
        self, tmp_path: Path, ota_config_files: OTAConfigFiles, expected: dict
    ):
        _ota_dir = _write_ota_dir(
            tmp_path / "boot/ota",
            {ECU_INFO_FNAME: OLD_ECU_INFO, PROXY_INFO_FNAME: OLD_PROXY_INFO},
        )

        install_ota_config_files(ota_config_files, dst_ota_dpath=_ota_dir)

        assert _read_ota_dir(_ota_dir) == expected

    def test_replace_symlink(self, tmp_path: Path):
        """A symlink at the destination is replaced, its target is not written."""
        _target = tmp_path / "target.yaml"
        _target.write_text(OLD_ECU_INFO)
        _ota_dir = tmp_path / "boot/ota"
        _ota_dir.mkdir(parents=True)
        (_ota_dir / ECU_INFO_FNAME).symlink_to(_target)

        install_ota_config_files(
            OTAConfigFiles(ecu_info=NEW_ECU_INFO), dst_ota_dpath=_ota_dir
        )

        assert not (_ota_dir / ECU_INFO_FNAME).is_symlink()
        assert (_ota_dir / ECU_INFO_FNAME).read_text() == NEW_ECU_INFO
        assert _target.read_text() == OLD_ECU_INFO

    def test_no_dst_dir(self, tmp_path: Path):
        _ota_dir = tmp_path / "boot/ota"

        install_ota_config_files(
            OTAConfigFiles(ecu_info=NEW_ECU_INFO, proxy_info=NEW_PROXY_INFO),
            dst_ota_dpath=_ota_dir,
        )

        assert _read_ota_dir(_ota_dir) == {
            ECU_INFO_FNAME: NEW_ECU_INFO,
            PROXY_INFO_FNAME: NEW_PROXY_INFO,
        }


class TestPreserveOTADirAndInstallConfigFiles:
    """For Jetson cboot/UEFI and RPi, which have /boot/ota in each slot's rootfs."""

    @pytest.fixture
    def slots(self, tmp_path: Path) -> tuple[Path, Path]:
        return tmp_path / "active", tmp_path / "standby"

    @pytest.mark.parametrize(
        "ota_config_files, expected_config_files",
        (
            (
                OTAConfigFiles(ecu_info=NEW_ECU_INFO, proxy_info=NEW_PROXY_INFO),
                {ECU_INFO_FNAME: NEW_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
            ),
            (
                OTAConfigFiles(proxy_info=NEW_PROXY_INFO),
                {ECU_INFO_FNAME: OLD_ECU_INFO, PROXY_INFO_FNAME: NEW_PROXY_INFO},
            ),
            (
                OTAConfigFiles(),
                {ECU_INFO_FNAME: OLD_ECU_INFO, PROXY_INFO_FNAME: OLD_PROXY_INFO},
            ),
        ),
    )
    def test_preserve(
        self,
        slots: tuple[Path, Path],
        ota_config_files: OTAConfigFiles,
        expected_config_files: dict,
    ):
        _active_mp, _standby_mp = slots
        _active_ota_dir = _write_ota_dir(
            _active_mp / "boot/ota",
            {
                ECU_INFO_FNAME: OLD_ECU_INFO,
                PROXY_INFO_FNAME: OLD_PROXY_INFO,
                "active_only_file": "active",
            },
        )

        preserve_ota_dir_and_install_config_files(
            active_slot_mp=_active_mp,
            standby_slot_mp=_standby_mp,
            ota_config_files=ota_config_files,
        )

        _res = _read_ota_dir(_standby_mp / "boot/ota")
        assert _res.pop("active_only_file") == "active"
        assert _res == expected_config_files
        # the active slot's /boot/ota is not touched
        assert _read_ota_dir(_active_ota_dir) == {
            ECU_INFO_FNAME: OLD_ECU_INFO,
            PROXY_INFO_FNAME: OLD_PROXY_INFO,
            "active_only_file": "active",
        }

    def test_nothing_left_from_the_image(self, slots: tuple[Path, Path]):
        """The standby's /boot/ota ends up as the active one, also when the OTA config
        files are persisted but the active slot doesn't have them."""
        _active_mp, _standby_mp = slots
        _write_ota_dir(_active_mp / "boot/ota", {"active_only_file": "active"})
        _standby_ota_dir = _write_ota_dir(
            _standby_mp / "boot/ota",
            {
                ECU_INFO_FNAME: NEW_ECU_INFO,
                PROXY_INFO_FNAME: NEW_PROXY_INFO,
                "image_only_file": "image",
            },
        )

        preserve_ota_dir_and_install_config_files(
            active_slot_mp=_active_mp,
            standby_slot_mp=_standby_mp,
            ota_config_files=OTAConfigFiles(),
        )

        assert _read_ota_dir(_standby_ota_dir) == {"active_only_file": "active"}

    def test_symlink_in_the_image_not_written_through(
        self, slots: tuple[Path, Path], tmp_path: Path
    ):
        """A symlink in the OTA image's /boot/ota is replaced, its target is untouched."""
        _active_mp, _standby_mp = slots
        _write_ota_dir(
            _active_mp / "boot/ota",
            {ECU_INFO_FNAME: OLD_ECU_INFO, PROXY_INFO_FNAME: OLD_PROXY_INFO},
        )
        _standby_ota_dir = _write_ota_dir(_standby_mp / "boot/ota", {})
        # an absolute symlink is resolved against the running rootfs
        _link_target = tmp_path / "running_rootfs_file"
        _link_target.write_text("running")
        (_standby_ota_dir / PROXY_INFO_FNAME).symlink_to(_link_target)

        preserve_ota_dir_and_install_config_files(
            active_slot_mp=_active_mp,
            standby_slot_mp=_standby_mp,
            ota_config_files=OTAConfigFiles(),
        )

        assert not (_standby_ota_dir / PROXY_INFO_FNAME).is_symlink()
        assert _read_ota_dir(_standby_ota_dir) == {
            ECU_INFO_FNAME: OLD_ECU_INFO,
            PROXY_INFO_FNAME: OLD_PROXY_INFO,
        }
        assert _link_target.read_text() == "running"

    def test_no_active_ota_dir(self, slots: tuple[Path, Path]):
        """Basically this should not happen, the standby's /boot/ota is left as it is."""
        _active_mp, _standby_mp = slots
        _standby_ota_dir = _write_ota_dir(
            _standby_mp / "boot/ota", {"image_only_file": "image"}
        )

        preserve_ota_dir_and_install_config_files(
            active_slot_mp=_active_mp,
            standby_slot_mp=_standby_mp,
            ota_config_files=OTAConfigFiles(ecu_info=NEW_ECU_INFO),
        )

        assert _read_ota_dir(_standby_ota_dir) == {"image_only_file": "image"}


class TestBackupAndRestore:
    def test_backup_and_restore(self, tmp_path: Path):
        _ota_dir = _write_ota_dir(tmp_path / "boot/ota", {ECU_INFO_FNAME: OLD_ECU_INFO})
        _ota_config_files = OTAConfigFiles(
            ecu_info=NEW_ECU_INFO, proxy_info=NEW_PROXY_INFO
        )

        _backup = backup_ota_config_files(_ota_dir, _ota_config_files)
        install_ota_config_files(_ota_config_files, dst_ota_dpath=_ota_dir)
        restore_ota_config_files(_ota_dir, _backup)

        # the existing file is restored, and the new one is removed
        assert _read_ota_dir(_ota_dir) == {ECU_INFO_FNAME: OLD_ECU_INFO}

    def test_backup_only_the_files_to_install(self, tmp_path: Path):
        _ota_dir = _write_ota_dir(
            tmp_path / "boot/ota",
            {ECU_INFO_FNAME: OLD_ECU_INFO, PROXY_INFO_FNAME: OLD_PROXY_INFO},
        )

        assert backup_ota_config_files(
            _ota_dir, OTAConfigFiles(proxy_info=NEW_PROXY_INFO)
        ) == {PROXY_INFO_FNAME: OLD_PROXY_INFO.encode()}


class _Stop(Exception):
    """Stop the post-update right after the step under test."""


class TestJetsonPostUpdateWiring:
    """The Jetson boot controllers pass the OTA config files to the preserve step."""

    OTA_CONFIG_FILES = OTAConfigFiles(ecu_info=NEW_ECU_INFO, proxy_info=NEW_PROXY_INFO)

    @pytest.fixture
    def mp_control(self, tmp_path: Path):
        return SimpleNamespace(
            active_slot_mount_point=tmp_path / "active",
            standby_slot_mount_point=tmp_path / "standby",
        )

    def test_jetson_uefi(self, mocker: MockerFixture, mp_control):
        mocker.patch.object(_jetson_uefi, "update_standby_slot_extlinux_cfg")
        _preserve = mocker.patch.object(
            _jetson_uefi,
            "preserve_ota_dir_and_install_config_files",
            side_effect=_Stop,
        )
        _ctrl = object.__new__(JetsonUEFIBootControl)
        _ctrl._mp_control = mp_control  # type: ignore[assignment]
        _ctrl._uefi_control = mocker.MagicMock()

        with pytest.raises(_Stop):
            _ctrl._post_update_platform_specific(
                update_version="2.0.0", ota_config_files=self.OTA_CONFIG_FILES
            )

        _preserve.assert_called_once_with(
            active_slot_mp=mp_control.active_slot_mount_point,
            standby_slot_mp=mp_control.standby_slot_mount_point,
            ota_config_files=self.OTA_CONFIG_FILES,
        )

    def test_jetson_cboot(self, mocker: MockerFixture, mp_control):
        mocker.patch.object(_jetson_cboot, "update_standby_slot_extlinux_cfg")
        _preserve = mocker.patch.object(
            _jetson_cboot,
            "preserve_ota_dir_and_install_config_files",
            side_effect=_Stop,
        )
        _ctrl = object.__new__(JetsonCBootControl)
        _ctrl._mp_control = mp_control  # type: ignore[assignment]
        _ctrl._cboot_control = mocker.MagicMock()
        _ctrl._firmware_update = mocker.MagicMock(return_value=None)  # type: ignore[method-assign]

        with pytest.raises(_Stop):
            _ctrl._post_update_platform_specific(
                update_version="2.0.0", ota_config_files=self.OTA_CONFIG_FILES
            )

        _preserve.assert_called_once_with(
            active_slot_mp=mp_control.active_slot_mount_point,
            standby_slot_mp=mp_control.standby_slot_mount_point,
            ota_config_files=self.OTA_CONFIG_FILES,
        )
