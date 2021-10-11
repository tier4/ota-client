import os
import pytest
from pathlib import Path

test_dir = Path(__file__).parent


def test_ota_client_status(mocker, tmp_path):
    from ota_client import OtaClient, OtaClientFailureType
    from ota_partition import OtaPartition, OtaPartitionFile

    """
    tmp_path/boot
            /boot/ota-partition
            /boot/ota-partition.sdx3
            /boot/ota-partition.sdx4
            /etc/fstab
            /mnt/standby/
    /dev/sdx
    /dev/sdx2 /boot
    /dev/sdx3 / (UUID: 01234567-0123-0123-0123-0123456789ab)
    /dev/sdx4 (unmounted) (UUID: 76543210-3210-3210-3210-ba9876543210)
    """
    # directory setup
    boot_dir = tmp_path / "boot"
    boot_dir.mkdir()
    ota_partition = boot_dir / "ota-partition"
    sdx3 = boot_dir / "ota-partition.sdx3"
    sdx4 = boot_dir / "ota-partition.sdx4"
    sdx3.mkdir()
    sdx4.mkdir()
    ota_partition.symlink_to("ota-partition.sdx3")
    (sdx4 / "status").write_text("SUCCESS")
    (sdx3 / "version").write_text("1.2.3")

    # file path patch
    mocker.patch.object(OtaPartition, "BOOT_DIR", boot_dir)

    # patch OtaPartition
    mocker.patch.object(OtaPartition, "_get_root_device_file", return_value="/dev/sdx3")
    mocker.patch.object(OtaPartition, "_get_boot_device_file", return_value="/dev/sdx2")
    mocker.patch.object(
        OtaPartition, "_get_parent_device_file", return_value="/dev/sdx"
    )
    mocker.patch.object(
        OtaPartition, "_get_standby_device_file", return_value="/dev/sdx4"
    )

    # test start
    ota_client = OtaClient()
    ota_client._total_regular_files = 99
    ota_client._regular_files_processed = 1

    result, ota_status = ota_client.status()
    assert result == OtaClientFailureType.NO_FAILURE
    assert ota_status == {
        "status": "SUCCESS",
        "failure_type": "NO_FAILURE",
        "failure_reason": "",
        "version": "1.2.3",
        "update_progress": {
            "phase": "INITIAL",
            "total_regular_files": 99,
            "regular_files_processed": 1,
        },
    }
