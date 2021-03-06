import os
import pytest
from pathlib import Path

test_dir = Path(__file__).parent


def test_ota_client_status(mocker, tmp_path):
    import ota_client
    from ota_client import OtaClientFailureType
    from grub_ota_partition import OtaPartition

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
    ota_client_instance = ota_client.OtaClient()
    # thread-safe modifying the storage
    with ota_client_instance._statistics.acquire_staging_storage() as staging_slot:
        staging_slot["total_regular_files"] = 99
        staging_slot["total_regular_file_size"] = 12345667890
        staging_slot["regular_files_processed"] = 1
        staging_slot["files_processed_copy"] = 80
        staging_slot["files_processed_link"] = 9
        staging_slot["files_processed_download"] = 10
        staging_slot["file_size_processed_copy"] = 1111
        staging_slot["file_size_processed_link"] = 2222
        staging_slot["file_size_processed_download"] = 3333
        staging_slot["elapsed_time_copy"] = 1.01
        staging_slot["elapsed_time_link"] = 2.02
        staging_slot["elapsed_time_download"] = 3.03
        staging_slot["errors_download"] = 10
        staging_slot["total_elapsed_time"] = 10001

    result, ota_status = ota_client_instance.status()
    assert result == OtaClientFailureType.NO_FAILURE
    assert ota_status == {
        "status": "SUCCESS",
        "failure_type": "NO_FAILURE",
        "failure_reason": "",
        "version": "1.2.3",
        "update_progress": {
            "phase": "INITIAL",
            "total_regular_files": 99,
            "total_regular_file_size": 12345667890,
            "regular_files_processed": 1,
            "files_processed_copy": 80,
            "files_processed_link": 9,
            "files_processed_download": 10,
            "file_size_processed_copy": 1111,
            "file_size_processed_link": 2222,
            "file_size_processed_download": 3333,
            "elapsed_time_copy": 1.01,
            "elapsed_time_link": 2.02,
            "elapsed_time_download": 3.03,
            "errors_download": 10,
            "total_elapsed_time": 10001,
        },
    }
