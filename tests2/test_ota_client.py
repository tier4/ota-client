import os
import pytest

from tests2.grub_cfg_params import (
    grub_cfg_params,
    grub_cfg_custom_cfg_params,
    grub_cfg_wo_submenu,
    UUID_A,
    UUID_B,
)

custom_cfg = """
menuentry 'Ubuntu, with Linux 5.4.0-73-generic' --class ubuntu --class gnu-linux --class gnu --class os $menuentry_id_option 'gnulinux-5.4.0-73-generic-advanced-01234567-0123-0123-0123-0123456789ab' {
	recordfail
	load_video
	gfxmode $linux_gfx_mode
	insmod gzio
	if [ x$grub_platform = xxen ]; then insmod xzio; insmod lzopio; fi
	insmod part_gpt
	insmod ext2
	set root='hd0,gpt2'
	if [ x$feature_platform_search_hint = xy ]; then
	  search --no-floppy --fs-uuid --set=root --hint-bios=hd0,gpt2 --hint-efi=hd0,gpt2 --hint-baremetal=ahci0,gpt2  ad35fc7d-d90f-4a98-84ae-fd65aff1f535
	else
	  search --no-floppy --fs-uuid --set=root ad35fc7d-d90f-4a98-84ae-fd65aff1f535
	fi
	echo	'Loading Linux 5.4.0-73-generic ...'
	linux	/vmlinuz-1.2.3-45 root=UUID=76543210-3210-3210-3210-ba9876543210 ro  quiet splash $vt_handoff
	echo	'Loading initial ramdisk ...'
	initrd	/initrd.img-1.2.3-45"""


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
    mocker.patch.object(OtaStatusControl, "BOOT_DIR", tmp_path / "boot")

    # patch GrubControl
    def mock__get_uuid(dummy1, device):
        if device == "sdx3":
            return "01234567-0123-0123-0123-0123456789ab"
        if device == "sdx4":
            return "76543210-3210-3210-3210-ba9876543210"

    mocker.patch.object(GrubControl, "_get_uuid", mock__get_uuid)
    mocker.patch.object(GrubControl, "reboot", return_value=0)
    _grub_reboot_mock = mocker.patch.object(
        GrubControl, "_grub_reboot_cmd", return_value=0
    )
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
    mocker.patch("grub_control.platform.release", return_value="5.4.0-73-generic")

    # test start
    ota_client = OtaClient()
    ota_client.update("123.x", "http://localhost:8080", "")

    # make sure boot ota-partition is switched to sdx4
    assert os.readlink(tmp_path / "boot" / "ota-partition") == "ota-partition.sdx4"

    # custom.cfg is created
    assert (tmp_path / "boot" / "grub" / "custom.cfg").is_file()
    # assert open(tmp_path / "boot" / "grub" / "custom.cfg").read() == custom_cfg

    # number of menuentry in grub_cfg_wo_submenu is 9
    _grub_reboot_mock.assert_called_once_with(9)
