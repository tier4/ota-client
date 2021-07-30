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


def test_OtaBoot__update_finalize_ecuinfo_file(mocker, tmp_path):
    import ota_boot
    import grub_control
    import ota_status
    import os
    import yaml

    ota_status_path = tmp_path / "ota_status"
    ota_status_path.write_text("NORMAL")

    grubctl_mock = mocker.Mock(spec=grub_control.GrubCtl)
    otastatus_mock = mocker.Mock(spec=ota_status.OtaStatus)
    mocker.patch("grub_control.GrubCtl", return_value=grubctl_mock)
    mocker.patch("ota_status.OtaStatus", return_value=otastatus_mock)

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
    "boot_state, confirm_bank, result_state, result_boot",
    [
        ("NORMAL", True, "NORMAL", "NORMAL_BOOT"),
        ("SWITCHA", True, "NORMAL", "SWITCH_BOOT"),
        ("SWITCHA", False, "UPDATE_FAIL", "SWITCH_BOOT_FAIL"),
        ("SWITCHB", True, "NORMAL", "SWITCH_BOOT"),
        ("SWITCHB", False, "UPDATE_FAIL", "SWITCH_BOOT_FAIL"),
        ("ROLLBACKA", True, "NORMAL", "ROLLBACK_BOOT"),
        ("ROLLBACKA", False, "ROLLBACK_FAIL", "ROLLBACK_BOOT_FAIL"),
        ("ROLLBACKB", True, "NORMAL", "ROLLBACK_BOOT"),
        ("ROLLBACKB", False, "ROLLBACK_FAIL", "ROLLBACK_BOOT_FAIL"),
        ("UPDATE", True, "UPDATE_FAIL", "UPDATE_INCOMPLETE"),
        ("PREPARED", True, "UPDATE_FAIL", "UPDATE_INCOMPLETE"),
        ("ROLLBACK", True, "ROLLBACK_FAIL", "ROLLBACK_INCOMPLETE"),
    ],
)
def test_OtaBoot__boot(
    mocker, tmp_path, boot_state, confirm_bank, result_state, result_boot
):
    import ota_boot
    import grub_control
    import ota_status

    def mock__confirm_banka(self):
        return confirm_bank

    def mock__confirm_bankb(self):
        return confirm_bank

    mocker.patch("ota_boot.OtaBoot._confirm_banka", mock__confirm_banka)
    mocker.patch("ota_boot.OtaBoot._confirm_bankb", mock__confirm_bankb)
    ota_status_path = tmp_path / "ota_status"
    ota_status_path.write_text(boot_state)
    ota_rollback_count_path = tmp_path / "ota_rollback_count"
    ota_rollback_count_path.write_text("0")

    # def mock__get_ota_status():
    #    return boot_state

    grubctl_mock = mocker.Mock(spec=grub_control.GrubCtl)
    # otastatus_mock = mocker.Mock(spec=ota_status.OtaStatus)
    mocker.patch("grub_control.GrubCtl", return_value=grubctl_mock)
    # mocker.patch("ota_status.OtaStatus", return_value=otastatus_mock)

    otaboot = ota_boot.OtaBoot(
        ota_status_file=str(ota_status_path),
        ota_rollback_file=str(ota_rollback_count_path),
    )
    res_boot, res_state = otaboot._boot(boot_state, noexec=True)
    assert res_boot == result_boot
    assert res_state == result_state


@pytest.mark.parametrize(
    "boot_state, confirm_bank, result_state, result_boot",
    [
        ("NORMAL", True, "NORMAL", "NORMAL_BOOT"),
        ("SWITCHA", True, "NORMAL", "SWITCH_BOOT"),
        ("SWITCHA", False, "UPDATE_FAIL", "SWITCH_BOOT_FAIL"),
        ("SWITCHB", True, "NORMAL", "SWITCH_BOOT"),
        ("SWITCHB", False, "UPDATE_FAIL", "SWITCH_BOOT_FAIL"),
        ("ROLLBACKA", True, "NORMAL", "ROLLBACK_BOOT"),
        ("ROLLBACKA", False, "ROLLBACK_FAIL", "ROLLBACK_BOOT_FAIL"),
        ("ROLLBACKB", True, "NORMAL", "ROLLBACK_BOOT"),
        ("ROLLBACKB", False, "ROLLBACK_FAIL", "ROLLBACK_BOOT_FAIL"),
        ("UPDATE", True, "UPDATE_FAIL", "UPDATE_INCOMPLETE"),
        ("PREPARED", True, "UPDATE_FAIL", "UPDATE_INCOMPLETE"),
        ("ROLLBACK", True, "ROLLBACK_FAIL", "ROLLBACK_INCOMPLETE"),
    ],
)
def test_OtaBoot_boot(
    mocker, tmp_path, boot_state, confirm_bank, result_state, result_boot
):
    import ota_boot
    import grub_control
    import ota_status

    def mock__confirm_banka(self):
        return confirm_bank

    def mock__confirm_bankb(self):
        return confirm_bank

    mocker.patch("ota_boot.OtaBoot._confirm_banka", mock__confirm_banka)
    mocker.patch("ota_boot.OtaBoot._confirm_bankb", mock__confirm_bankb)
    ota_status_path = tmp_path / "ota_status"
    ota_status_path.write_text(boot_state)
    ota_rollback_count_path = tmp_path / "ota_rollback_count"
    ota_rollback_count_path.write_text("0")

    # def mock__get_ota_status():
    #    return boot_state

    grubctl_mock = mocker.Mock(spec=grub_control.GrubCtl)
    # otastatus_mock = mocker.Mock(spec=ota_status.OtaStatus)
    mocker.patch("grub_control.GrubCtl", return_value=grubctl_mock)
    # mocker.patch("ota_status.OtaStatus", return_value=otastatus_mock)

    otaboot = ota_boot.OtaBoot(
        ota_status_file=str(ota_status_path),
        ota_rollback_file=str(ota_rollback_count_path),
    )
    res_boot = otaboot.boot()
    assert res_boot == result_boot
