import pytest

from tests2.grub_cfg_params import (
    grub_cfg_params,
    grub_cfg_custom_cfg_params,
    grub_cfg_wo_submenu,
    UUID_A,
    UUID_B,
)


def test_ota_client(mocker, tmp_path):
    from ota_client import OtaClient
    from ota_partition import OtaPartition
    from ota_status import OtaStatusControl
    from grub_control import GrubControl
    import grub_control

    # patch OtaPartition
    mocker.patch.object(OtaPartition, "_get_root_device_file", return_value="/dev/sdx3")
    mocker.patch.object(OtaPartition, "_get_boot_device_file", return_value="/dev/sdx2")
    mocker.patch.object(
        OtaPartition, "_get_parent_device_file", return_value="/dev/sdx"
    )
    mocker.patch.object(
        OtaPartition, "_get_standby_device_file", return_value="/dev/sdx4"
    )

    mocker.patch.object(
        OtaPartition,
        "BOOT_OTA_PARTITION_FILE",
        tmp_path / "boot" / "ota-partition",
    )

    boot_dir = tmp_path / "boot"
    boot_dir.mkdir(exist_ok=True)
    ota_partition = boot_dir / "ota-partition"
    sdx3 = boot_dir / "ota-partition.sdx3"
    sdx4 = boot_dir / "ota-partition.sdx4"
    sdx3.mkdir()
    sdx4.mkdir()
    ota_partition.symlink_to("ota-partition.sdx3")

    # patch OtaClient
    mocker.patch.object(OtaClient, "MOUNT_POINT", tmp_path / "mnt" / "standby")
    mount_dir = tmp_path / "mnt"
    mount_dir.mkdir()
    standby_dir = mount_dir / "standby"
    standby_dir.mkdir()

    # patch OtaStatusControl
    mocker.patch.object(OtaStatusControl, "_mount_cmd", return_value=0)

    # patch GrubControl
    def mock__get_uuid(dummy1, device):
        if device == "sdx3":
            return "01234567-0123-0123-0123-0123456789ab"
        if device == "sdx4":
            return "76543210-3210-3210-3210-ba9876543210"

    mocker.patch.object(GrubControl, "_get_uuid", mock__get_uuid)
    mocker.patch.object(GrubControl, "reboot", return_value=0)
    mocker.patch.object(
        GrubControl, "GRUB_CFG_FILE", tmp_path / "boot" / "grub" / "grub.cfg"
    )
    mocker.patch.object(
        GrubControl, "CUSTOM_CFG_FILE", tmp_path / "boot" / "grub" / "custom.cfg"
    )
    boot_dir = tmp_path / "boot"
    boot_dir.mkdir(exist_ok=True)
    grub_dir = boot_dir / "grub"
    grub_dir.mkdir()
    grub_cfg = grub_dir / "grub.cfg"
    grub_cfg.write_text(grub_cfg_wo_submenu)
    mocker.patch("grub_control.platform.release", return_value="5.4.0-74-generic")

    # test start
    ota_client = OtaClient()
    ota_client.update("123.x", "http://localhost:8080/20210817050734-1", "")
