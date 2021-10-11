import os
import pytest
import yaml


@pytest.mark.parametrize(
    "ecu_info_dict, secondary_ecus, ecu_id",
    (
        (None, [], "autoware"),
        ({"format_version": 1, "ecu_id": "autoware"}, [], "autoware"),
        (
            {
                "format_version": 1,
                "ecu_id": "autoware",
                "secondaries": [
                    {"ecu_id": "perception1", "ip_addr": "192.168.0.11"},
                    {"ecu_id": "perception2", "ip_addr": "192.168.0.12"},
                ],
            },
            [
                {"ecu_id": "perception1", "ip_addr": "192.168.0.11"},
                {"ecu_id": "perception2", "ip_addr": "192.168.0.12"},
            ],
            "autoware",
        ),
    ),
)
def test_ecu_info(mocker, tmp_path, ecu_info_dict, secondary_ecus, ecu_id):
    from ecu_info import EcuInfo

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


@pytest.mark.parametrize(
    "ecu_info_dict",
    (
        ({"format_version": 2, "ecu_id": "autoware"}),
        ({"ecu_id": "autoware"}),
    ),
)
def test_ecu_info_exception(mocker, tmp_path, ecu_info_dict):
    from ecu_info import EcuInfo
    from ota_error import OtaErrorUnrecoverable

    boot_dir = tmp_path / "boot"
    boot_dir.mkdir()
    (boot_dir / "ota").mkdir()
    ecu_info_file = boot_dir / "ota" / "ecu_info.yaml"
    if ecu_info_dict is not None:
        ecu_info_file.write_text(yaml.dump(ecu_info_dict))

    mocker.patch.object(EcuInfo, "ECU_INFO_FILE", ecu_info_file)
    with pytest.raises(OtaErrorUnrecoverable):
        ecu_info = EcuInfo()
