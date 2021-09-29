import os
import pytest
from pathlib import Path

test_dir = Path(__file__).parent


def test_ota_client_status(mocker, tmp_path):
    from ota_client import OtaClient, OtaClientResult
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
    (sdx4 / "version").write_text("1.2.3")

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
    result, ota_status = ota_client.status()
    assert result == OtaClientResult.OK
    assert ota_status == {
        "status": "SUCCESS",
        "failure_type": "OK",
        "failure_reason": "",
        "version": "1.2.3",
        "update_progress": {  # TODO
            "phase": "",
            "total_regular_files": 0,
            "regular_files_processed": 0,
        },
        "rollback_progress": {  # TODO
            "phase": "",
        },
    }
