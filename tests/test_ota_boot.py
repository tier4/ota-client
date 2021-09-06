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
    "boot_state, confirm_banka, confirm_bankb, result_state, result_boot, callcnt_finalize_update, callcnt_finalize_rollback",
    [
        ("NORMAL", True, False, "NORMAL", "NORMAL_BOOT", 0, 0),
        ("SWITCHA", True, False, "NORMAL", "SWITCH_BOOT", 1, 0),
        ("SWITCHA", False, True, "UPDATE_FAIL", "SWITCH_BOOT_FAIL", 0, 0),
        ("SWITCHB", False, True, "NORMAL", "SWITCH_BOOT", 1, 0),
        ("SWITCHB", True, False, "UPDATE_FAIL", "SWITCH_BOOT_FAIL", 0, 0),
        ("ROLLBACKA", True, False, "NORMAL", "ROLLBACK_BOOT", 0, 1),
        ("ROLLBACKA", False, True, "ROLLBACK_FAIL", "ROLLBACK_BOOT_FAIL", 0, 0),
        ("ROLLBACKB", False, True, "NORMAL", "ROLLBACK_BOOT", 0, 1),
        ("ROLLBACKB", True, False, "ROLLBACK_FAIL", "ROLLBACK_BOOT_FAIL", 0, 0),
        ("UPDATE", True, False, "UPDATE_FAIL", "UPDATE_INCOMPLETE", 0, 0),
        ("PREPARED", True, False, "UPDATE_FAIL", "UPDATE_INCOMPLETE", 0, 0),
        ("ROLLBACK", True, False, "ROLLBACK_FAIL", "ROLLBACK_INCOMPLETE", 0, 0),
    ],
)
def test_OtaBoot__boot(
    mocker,
    tmp_path,
    boot_state,
    confirm_banka,
    confirm_bankb,
    result_state,
    result_boot,
    callcnt_finalize_update,
    callcnt_finalize_rollback,
):
    import ota_boot
    import grub_control

    def mock__confirm_banka(self):
        return confirm_banka

    def mock__confirm_bankb(self):
        return confirm_bankb

    def mock__finalize_update(self):
        return

    def mock__finalize_rollback(self):
        return

    mocker.patch("ota_boot.OtaBoot._confirm_banka", mock__confirm_banka)
    mocker.patch("ota_boot.OtaBoot._confirm_bankb", mock__confirm_bankb)

    ota_status_path = tmp_path / "ota_status"
    ota_status_path.write_text(boot_state)
    ota_rollback_count_path = tmp_path / "ota_rollback_count"
    ota_rollback_count_path.write_text("0")

    grubctl_mock = mocker.Mock(spec=grub_control.GrubCtl)
    mocker.patch("grub_control.GrubCtl", return_value=grubctl_mock)

    otaboot = ota_boot.OtaBoot(
        ota_status_file=str(ota_status_path),
        ota_rollback_file=str(ota_rollback_count_path),
    )

    finalize_update_mock = mocker.patch.object(otaboot, "_finalize_update")
    finalize_rollback_mock = mocker.patch.object(otaboot, "_finalize_rollback")

    res_boot, res_state = otaboot._boot(boot_state)
    assert res_boot == result_boot
    assert res_state == result_state
    assert finalize_update_mock.call_count == callcnt_finalize_update
    assert finalize_rollback_mock.call_count == callcnt_finalize_rollback


@pytest.mark.parametrize(
    "boot_state, confirm_banka, confirm_bankb, result_state, result_boot, callcnt_finalize_update, callcnt_finalize_rollback",
    [
        ("NORMAL", True, False, "NORMAL", "NORMAL_BOOT", 0, 0),
        ("SWITCHA", True, False, "NORMAL", "SWITCH_BOOT", 1, 0),
        ("SWITCHA", False, True, "UPDATE_FAIL", "SWITCH_BOOT_FAIL", 0, 0),
        ("SWITCHB", False, True, "NORMAL", "SWITCH_BOOT", 1, 0),
        ("SWITCHB", True, False, "UPDATE_FAIL", "SWITCH_BOOT_FAIL", 0, 0),
        ("ROLLBACKA", True, False, "NORMAL", "ROLLBACK_BOOT", 0, 1),
        ("ROLLBACKA", False, True, "ROLLBACK_FAIL", "ROLLBACK_BOOT_FAIL", 0, 0),
        ("ROLLBACKB", False, True, "NORMAL", "ROLLBACK_BOOT", 0, 1),
        ("ROLLBACKB", True, False, "ROLLBACK_FAIL", "ROLLBACK_BOOT_FAIL", 0, 0),
        ("UPDATE", True, False, "UPDATE_FAIL", "UPDATE_INCOMPLETE", 0, 0),
        ("PREPARED", True, False, "UPDATE_FAIL", "UPDATE_INCOMPLETE", 0, 0),
        ("ROLLBACK", True, False, "ROLLBACK_FAIL", "ROLLBACK_INCOMPLETE", 0, 0),
    ],
)
def test_OtaBoot_boot(
    mocker,
    tmp_path,
    boot_state,
    confirm_banka,
    confirm_bankb,
    result_state,
    result_boot,
    callcnt_finalize_update,
    callcnt_finalize_rollback,
):
    import ota_boot
    import grub_control

    def mock__confirm_banka(self):
        return confirm_banka

    def mock__confirm_bankb(self):
        return confirm_bankb

    mocker.patch("ota_boot.OtaBoot._confirm_banka", mock__confirm_banka)
    mocker.patch("ota_boot.OtaBoot._confirm_bankb", mock__confirm_bankb)

    ota_status_path = tmp_path / "ota_status"
    ota_status_path.write_text(boot_state)
    ota_rollback_count_path = tmp_path / "ota_rollback_count"
    ota_rollback_count_path.write_text("0")

    grubctl_mock = mocker.Mock(spec=grub_control.GrubCtl)
    mocker.patch("grub_control.GrubCtl", return_value=grubctl_mock)

    otaboot = ota_boot.OtaBoot(
        ota_status_file=str(ota_status_path),
        ota_rollback_file=str(ota_rollback_count_path),
    )
    finalize_update_mock = mocker.patch.object(otaboot, "_finalize_update")
    finalize_rollback_mock = mocker.patch.object(otaboot, "_finalize_rollback")

    res_boot = otaboot.boot()
    assert res_boot == result_boot
    assert finalize_update_mock.call_count == callcnt_finalize_update
    assert finalize_rollback_mock.call_count == callcnt_finalize_rollback
