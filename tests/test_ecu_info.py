from pathlib import Path
import pytest
from pytest_mock import MockerFixture
import yaml


@pytest.mark.parametrize(
    "ecu_info_dict, secondary_ecus, ecu_id, ip_addr, available_ecu_ids",
    (
        (None, [], "autoware", "localhost", ["autoware"]),
        (
            {"format_version": 1, "ecu_id": "autoware"},
            [],
            "autoware",
            "localhost",
            ["autoware"],
        ),
        (
            {"format_version": 2, "ecu_id": "autoware", "ip_addr": "192.168.1.1"},
            [],
            "autoware",
            "192.168.1.1",
            ["autoware"],
        ),
        (
            {
                "format_version": 1,
                "ecu_id": "autoware",
                "secondaries": [
                    {"ecu_id": "perception1", "ip_addr": "192.168.0.11"},
                    {"ecu_id": "perception2", "ip_addr": "192.168.0.12"},
                ],
                "available_ecu_ids": ["autoware", "perception1", "perception2"],
            },
            [
                {"ecu_id": "perception1", "ip_addr": "192.168.0.11"},
                {"ecu_id": "perception2", "ip_addr": "192.168.0.12"},
            ],
            "autoware",
            "localhost",
            ["autoware", "perception1", "perception2"],
        ),
    ),
)
def test_ecu_info(
    mocker: MockerFixture,
    tmp_path: Path,
    ecu_info_dict,
    secondary_ecus,
    ecu_id,
    ip_addr,
    available_ecu_ids,
):
    from otaclient.app.ecu_info import EcuInfo

    boot_dir = tmp_path / "boot"
    boot_dir.mkdir()
    (boot_dir / "ota").mkdir()
    ecu_info_file = boot_dir / "ota" / "ecu_info.yaml"
    if ecu_info_dict is not None:
        ecu_info_file.write_text(yaml.dump(ecu_info_dict))

    mocker.patch.object(EcuInfo, "ECU_INFO_FILE", ecu_info_file)
    ecu_info = EcuInfo()
    assert ecu_info.get_secondary_ecus() == secondary_ecus
    assert ecu_info.get_ecu_id() == ecu_id
    assert ecu_info.get_ecu_ip_addr() == ip_addr
    assert ecu_info.get_available_ecu_ids() == available_ecu_ids
