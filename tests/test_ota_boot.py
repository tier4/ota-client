#!/usr/bin/env python3
import pytest

ECUINFO = """\
main_ecu:
  ecu_name: 'autoware_ecu'
  ecu_type: 'autoware'
  ecu_id: '1'
  version: '0.0.1'
  independent: True
  ip_addr: '192.168.10.2'
"""

ECUINFO_UPDATE = """\
main_ecu:
  ecu_name: 'autoware_ecu'
  ecu_type: 'autoware'
  ecu_id: '1'
  version: '0.1.0'
  independent: True
  ip_addr: '192.168.10.2'
"""


def test__gen_ota_status_file(tmp_path):
    import ota_boot

    ota_status_path = tmp_path / "ota_status"
    ota_boot._gen_ota_status_file(ota_status_path)
    assert ota_status_path.read_text() == "NORMAL"


def test_OtaBoot__update_finalize_ecuinfo_file(mocker, tmp_path):
    import ota_boot
    import grub_control
    import os
    import yaml

    ota_status_path = tmp_path / "ota_status"
    ota_status_path.write_text("NORMAL")

    grubctl_mock = mocker.Mock(spec=grub_control.GrubCtl)
    mocker.patch("grub_control.GrubCtl", return_value=grubctl_mock)

    ecuinfo_path = tmp_path / "ecuinfo.yaml"
    ecuinfo_update_path = tmp_path / "ecuinfo.yaml.update"
    ecuinfo_path.write_text(ECUINFO)
    ecuinfo_update_path.write_text(ECUINFO_UPDATE)

    otaboot = ota_boot.OtaBoot(ecuinfo_yaml_file=str(ecuinfo_path))
    otaboot._update_finalize_ecuinfo_file()

    ecuinfo = ecuinfo_path.read_text()
    assert os.path.isfile(str(ecuinfo_path))
    with open(ecuinfo_path, "r") as f:
        ecuinfo = yaml.load(f, Loader=yaml.SafeLoader)
    print(f"ecuinfo: {ecuinfo}")
    assert ecuinfo["main_ecu"]["version"] == "0.1.0"


@pytest.mark.parametrize(
    "boot_state, confirm_bank, result",
    [
        ("NORMAL", True, "NORMAL_BOOT"),
        ("SWITCHA", True, "SWITCH_BOOT"),
        ("SWITCHA", False, "SWITCH_BOOT_FAIL"),
        ("SWITCHB", True, "SWITCH_BOOT"),
        ("SWITCHB", False, "SWITCH_BOOT_FAIL"),
        ("ROLLBACKA", True, "ROLLBACK_BOOT"),
        ("ROLLBACKA", False, "ROLLBACK_BOOT_FAIL"),
        ("ROLLBACKB", True, "ROLLBACK_BOOT"),
        ("ROLLBACKB", False, "ROLLBACK_BOOT_FAIL"),
        ("ROLLBACK", True, "ROLLBACK_IMCOMPLETE"),
        ("UPDATE", True, "UPDATE_IMCOMPLETE"),
    ],
)
def test_OtaBoot__boot(mocker, tmp_path, boot_state, confirm_bank, result):
    import ota_boot
    import grub_control

    def mock__confirm_banka(self):
        return confirm_bank

    def mock__confirm_bankb(self):
        return confirm_bank

    mocker.patch("ota_boot.OtaBoot._confirm_banka", mock__confirm_banka)
    mocker.patch("ota_boot.OtaBoot._confirm_bankb", mock__confirm_bankb)
    ota_status_path = tmp_path / "ota_status"
    ota_status_path.write_text(boot_state)

    grubctl_mock = mocker.Mock(spec=grub_control.GrubCtl)
    mocker.patch("grub_control.GrubCtl", return_value=grubctl_mock)

    otaboot = ota_boot.OtaBoot(ota_status_file=str(ota_status_path))
    assert otaboot._boot(noexec=True) == result
    assert ota_status_path.read_text() == "NORMAL"
