import pytest


@pytest.mark.parametrize(
    "ecu_status, result_ecu_status_pb2",
    [
        ("NORMAL", 0),  # NORMAL
        ("UPDATE", 1),  # UPDATING
        ("PREPARED", 2),  # DOWNLOADED
        ("SWITCHA", 4),  # REBOOT
        ("SWITCHB", 4),  # REBOOT
        ("ROLLBACK", 3),  # ROLLBACK
        ("ROLLBACKA", 4),  # REBOOT
        ("ROLLBACKB", 4),  # REBOOT
        ("UPDATE_FAIL", 5),  # UPDATE ERROR
        ("ROLLBACK_FAIL", 6),  # ROLLBACK ERROR
        ("", 7),  # UNKNOWN
    ],
)
def test_OtaClientService__conv_ecu_status(ecu_status, result_ecu_status_pb2):
    import ota_client_service

    assert (
        ota_client_service.OtaClientService._conv_ecu_status(ecu_status)
        == result_ecu_status_pb2
    )


@pytest.mark.parametrize(
    "boot_status, result_boot_status_pb2",
    [
        ("NORMAL_BOOT", 0),  # NORMAL_BOOT
        ("SWITCH_BOOT", 1),  # SWITCH_BOOT
        ("SWITCH_BOOT_FAIL", 3),  # SWITCH_BOOT_FAIL
        ("ROLLBACK_BOOT", 2),  # ROLLBACK_BOOT
        ("ROLLBACK_BOOT_FAIL", 4),  # ROLLBACK_BOOT_FAIL
        ("UPDATE_INCOMPLETE", 5),  # UPDATE_INCOMPLETE
        ("ROLLBACK_INCOMPLETE", 6),  # ROLLBACK_INCOMPLETE
        ("", 7),  # UNKNOWN
    ],
)
def test_OtaClientService__conv_ecu_boot_status(boot_status, result_boot_status_pb2):
    import ota_client_service

    assert (
        ota_client_service.OtaClientService._conv_ecu_boot_status(boot_status)
        == result_boot_status_pb2
    )
