import pytest


def test_ota_client(mocker, tmp_path):
    from ota_client import OtaClient
    import ota_partition
    import ota_status
    import grub_control

    mocker.patch.object(
        ota_partition.OtaPartition, "_get_root_device_file", return_value="/dev/sdx3"
    )
    mocker.patch.object(
        ota_partition.OtaPartition, "_get_boot_device_file", return_value="/dev/sdx2"
    )
    mocker.patch.object(
        ota_partition.OtaPartition, "_get_parent_device_file", return_value="/dev/sdx"
    )
    mocker.patch.object(
        ota_partition.OtaPartition, "_get_standby_device_file", return_value="/dev/sdx4"
    )

    mocker.patch.object(
        ota_partition.OtaPartition,
        "BOOT_OTA_PARTITION_FILE",
        tmp_path / "boot" / "ota-partition",
    )

    boot_dir = tmp_path / "boot"
    boot_dir.mkdir()
    ota_partition = boot_dir / "ota-partition"
    sdx3 = boot_dir / "ota-partition.sdx3"
    sdx4 = boot_dir / "ota-partition.sdx4"
    sdx3.mkdir()
    sdx4.mkdir()
    ota_partition.symlink_to("ota-partition.sdx3")

    # mount
    mocker.patch.object(OtaClient, "MOUNT_POINT", tmp_path / "mnt" / "standby")
    mount_dir = tmp_path / "mnt"
    mount_dir.mkdir()
    standby_dir = mount_dir / "standby"
    standby_dir.mkdir()
    mocker.patch.object(ota_status.OtaStatusControl, "_mount_cmd", return_value=0)

    mocker.patch.object(grub_control.GrubControl, "reboot", return_value=0)

    def mock__get_uuid(dummy1, device):
        if device == "sdx3":
            return "01234567-0123-0123-0123-0123456789ab"
        if device == "sdx4":
            return "76543210-3210-3210-3210-ba9876543210"

    mocker.patch.object(grub_control.GrubControl, "_get_uuid", mock__get_uuid)

    mocker.patch.object(grub_control.GrubControl, "reboot", return_value=0)

    ota_client = OtaClient()
    ota_client.update("123.x", "http://localhost:8080/20210817050734-1", "")
