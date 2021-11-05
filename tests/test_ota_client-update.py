import os
import pytest
import time
import json
import shutil
import requests
import requests_mock
from pathlib import Path

test_dir = Path(__file__).parent

grub_cfg_wo_submenu = open(test_dir / "grub.cfg.wo_submenu").read()
custom_cfg = open(test_dir / "custom.cfg").read()


FSTAB_DEV_DISK_BY_UUID = """\
# /etc/fstab: static file system information.
#
# Use 'blkid' to print the universally unique identifier for a
# device; this may be used with UUID= as a more robust way to name devices
# that works even if disks are added and removed. See fstab(5).
#
# <file system> <mount point>   <type>  <options>       <dump>  <pass>
# / was on /dev/sda3 during curtin installation
/dev/disk/by-uuid/01234567-0123-0123-0123-0123456789ab / ext4 defaults 0 0
# /boot was on /dev/sda2 during curtin installation
/dev/disk/by-uuid/cc59073d-9e5b-41e1-b724-576259341132 /boot ext4 defaults 0 0
/swap.img	none	swap	sw	0	0
"""

FSTAB_DEV_DISK_BY_UUID_STANDBY = """\
# /etc/fstab: static file system information.
#
# Use 'blkid' to print the universally unique identifier for a
# device; this may be used with UUID= as a more robust way to name devices
# that works even if disks are added and removed. See fstab(5).
#
# <file system> <mount point>   <type>  <options>       <dump>  <pass>
# / was on /dev/sda3 during curtin installation
/dev/disk/by-uuid/76543210-3210-3210-3210-ba9876543210 / ext4 defaults 0 0
# /boot was on /dev/sda2 during curtin installation
/dev/disk/by-uuid/cc59073d-9e5b-41e1-b724-576259341132 /boot ext4 defaults 0 0
/swap.img	none	swap	sw	0	0
"""

DEFAULT_GRUB = """\
# If you change this file, run 'update-grub' afterwards to update
# /boot/grub/grub.cfg.
# For full documentation of the options in this file, see:
#   info -f grub -n 'Simple configuration'

GRUB_DEFAULT=0
GRUB_TIMEOUT_STYLE=hidden
GRUB_TIMEOUT=10
GRUB_DISTRIBUTOR=`lsb_release -i -s 2> /dev/null || echo Debian`
GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"
GRUB_CMDLINE_LINUX=""

# Uncomment to enable BadRAM filtering, modify to suit your needs
# This works with Linux (no patch required) and with any kernel that obtains
# the memory map information from GRUB (GNU Mach, kernel of FreeBSD ...)
#GRUB_BADRAM="0x01234567,0xfefefefe,0x89abcdef,0xefefefef"

# Uncomment to disable graphical terminal (grub-pc only)
#GRUB_TERMINAL=console

# The resolution used on graphical terminal
# note that you can use only modes which your graphic card supports via VBE
# you can see them in real GRUB with the command `vbeinfo'
#GRUB_GFXMODE=640x480

# Uncomment if you don't want GRUB to pass "root=UUID=xxx" parameter to Linux
#GRUB_DISABLE_LINUX_UUID=true

# Uncomment to disable generation of recovery mode menu entries
#GRUB_DISABLE_RECOVERY="true"

# Uncomment to get a beep at grub start
#GRUB_INIT_TUNE="480 440 1"\
"""


def test_ota_client_update(mocker, tmp_path):
    from ota_client import OtaClient, OtaClientFailureType
    from ota_partition import OtaPartition, OtaPartitionFile
    from ota_status import OtaStatus
    from grub_control import GrubControl

    """
    tmp_path/boot
            /boot/grub/
            /boot/grub/grub.cfg -> ../ota-partition/grub.cfg
            /boot/grub/custom.cfg
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
    (sdx4 / "status").write_text("INITIALIZED")
    (sdx3 / "version").write_text("a.b.c")

    mount_dir = tmp_path / "mnt"
    mount_dir.mkdir()

    grub_dir = boot_dir / "grub"
    grub_dir.mkdir()
    grub_cfg = grub_dir / "grub.cfg"
    grub_cfg.symlink_to(Path("..") / "ota-partition" / "grub.cfg")
    grub_cfg.write_text(grub_cfg_wo_submenu)

    etc_dir = tmp_path / "etc"
    etc_dir.mkdir()
    fstab = etc_dir / "fstab"
    fstab.write_text(FSTAB_DEV_DISK_BY_UUID)
    default_dir = etc_dir / "default"
    default_dir.mkdir()

    # file path patch
    mocker.patch.object(OtaPartition, "BOOT_DIR", boot_dir)
    mocker.patch.object(OtaClient, "MOUNT_POINT", tmp_path / "mnt" / "standby")
    mocker.patch.object(GrubControl, "GRUB_CFG_FILE", boot_dir / "grub" / "grub.cfg")
    mocker.patch.object(
        GrubControl, "CUSTOM_CFG_FILE", boot_dir / "grub" / "custom.cfg"
    )
    mocker.patch.object(GrubControl, "FSTAB_FILE", tmp_path / "etc" / "fstab")
    mocker.patch.object(GrubControl, "DEFAULT_GRUB_FILE", etc_dir / "default" / "grub")

    # patch OtaPartition
    mocker.patch.object(OtaPartition, "_get_root_device_file", return_value="/dev/sdx3")
    mocker.patch.object(OtaPartition, "_get_boot_device_file", return_value="/dev/sdx2")
    mocker.patch.object(
        OtaPartition, "_get_parent_device_file", return_value="/dev/sdx"
    )
    mocker.patch.object(
        OtaPartition, "_get_standby_device_file", return_value="/dev/sdx4"
    )

    # patch OtaPartitionFile
    mocker.patch.object(OtaPartitionFile, "_mount_cmd", return_value=0)

    # patch GrubControl
    def mock__get_uuid(dummy1, device):
        if device == "sdx3":
            return "01234567-0123-0123-0123-0123456789ab"
        if device == "sdx4":
            return "76543210-3210-3210-3210-ba9876543210"

    mocker.patch.object(GrubControl, "_get_uuid", mock__get_uuid)
    cmdline = "BOOT_IMAGE=/vmlinuz-5.4.0-73-generic root=UUID=01234567-0123-0123-0123-0123456789ab ro maybe-ubiquity"

    mocker.patch.object(GrubControl, "_get_cmdline", return_value=cmdline)
    reboot_mock = mocker.patch.object(GrubControl, "reboot", return_value=0)
    _grub_reboot_mock = mocker.patch.object(
        GrubControl, "_grub_reboot_cmd", return_value=0
    )
    # test start
    ota_client = OtaClient()
    ota_client.update(
        "123.x", "http://ota-server:8080/ota-server", json.dumps({"test": "my-cookie"})
    )

    result, status = ota_client.status()
    assert result == OtaClientFailureType.NO_FAILURE
    assert status["status"] == "UPDATING"
    assert status["failure_type"] == "NO_FAILURE"
    assert status["failure_reason"] == ""
    assert status["version"] == "a.b.c"
    progress = status["update_progress"]
    assert progress["phase"] == "POST_PROCESSING"
    # NOTE: numbers are depends on ota-image
    # total file size processed is:
    # find data/ -type f | xargs ls -l | awk '{total += $5}; END {print total}'
    TOTAL_FILES = 2499
    # NOTE: There is difference between github actins and local environment, so
    # approximate value is used.
    TOTAL_FILE_SIZE_APPROX = 108700000
    assert progress["total_regular_files"] == TOTAL_FILES
    assert progress["regular_files_processed"] == progress["total_regular_files"]
    assert (
        progress["files_processed_copy"]
        + progress["files_processed_link"]
        + progress["files_processed_download"]
        == progress["total_regular_files"]
    )
    assert (
        progress["file_size_processed_copy"]
        + progress["file_size_processed_link"]
        + progress["file_size_processed_download"]
    ) // 100000 == TOTAL_FILE_SIZE_APPROX // 100000
    assert type(progress["elapsed_time_copy"]) == int  # in milliseconds
    assert type(progress["elapsed_time_link"]) == int  # in milliseconds
    assert type(progress["elapsed_time_download"]) == int  # in milliseconds

    # make sure boot ota-partition is NOT switched
    assert os.readlink(boot_dir / "ota-partition") == "ota-partition.sdx3"
    assert (
        os.readlink(boot_dir / "vmlinuz-ota.standby")
        == "ota-partition.sdx4/vmlinuz-ota"
    )
    assert (
        os.readlink(boot_dir / "initrd.img-ota.standby")
        == "ota-partition.sdx4/initrd.img-ota"
    )

    assert (
        os.readlink(boot_dir / "ota-partition.sdx4" / "vmlinuz-ota")
        == "vmlinuz-5.8.0-53-generic"  # FIXME
    )
    assert (
        os.readlink(boot_dir / "ota-partition.sdx4" / "initrd.img-ota")
        == "initrd.img-5.8.0-53-generic"  # FIXME
    )
    assert open(boot_dir / "ota-partition.sdx4" / "status").read() == "UPDATING"
    assert open(boot_dir / "ota-partition.sdx4" / "version").read() == "123.x"
    # make sure grub.cfg is not created yet in standby boot partition
    assert not (boot_dir / "ota-partition.sdx4" / "grub.cfg").is_file()

    # custom.cfg is created
    assert (boot_dir / "grub" / "custom.cfg").is_file()
    assert open(boot_dir / "grub" / "custom.cfg").read() == custom_cfg

    # number of menuentry in grub_cfg_wo_submenu is 9
    _grub_reboot_mock.assert_called_once_with(9)
    reboot_mock.assert_called_once()

    # fstab
    assert (
        open(tmp_path / "mnt" / "standby" / "etc" / "fstab").read()
        == FSTAB_DEV_DISK_BY_UUID_STANDBY
    )
    assert ota_client._ota_status.get_ota_status() == OtaStatus.UPDATING


@pytest.mark.parametrize(
    "error_injection, failure_reason_startswith",
    [
        ({"status_code": 403}, "requests error: status_code=403"),
        (
            {"exc": requests.exceptions.ConnectTimeout},
            "requests timeout or connection error:",
        ),
    ],
)
def test_ota_client_update_regular_download_error(
    mocker, tmp_path, error_injection, failure_reason_startswith
):
    from ota_client import OtaClient, OtaClientFailureType
    from ota_partition import OtaPartition, OtaPartitionFile
    from ota_status import OtaStatus
    from grub_control import GrubControl

    """
    tmp_path/boot
            /boot/grub/
            /boot/grub/grub.cfg -> ../ota-partition/grub.cfg
            /boot/grub/custom.cfg
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
    (sdx4 / "status").write_text("INITIALIZED")
    (sdx3 / "version").write_text("a.b.c")

    mount_dir = tmp_path / "mnt"
    mount_dir.mkdir()

    grub_dir = boot_dir / "grub"
    grub_dir.mkdir()
    grub_cfg = grub_dir / "grub.cfg"
    grub_cfg.symlink_to(Path("..") / "ota-partition" / "grub.cfg")
    grub_cfg.write_text(grub_cfg_wo_submenu)

    etc_dir = tmp_path / "etc"
    etc_dir.mkdir()
    fstab = etc_dir / "fstab"
    fstab.write_text(FSTAB_DEV_DISK_BY_UUID)
    default_dir = etc_dir / "default"
    default_dir.mkdir()

    # file path patch
    mocker.patch.object(OtaPartition, "BOOT_DIR", boot_dir)
    mocker.patch.object(OtaClient, "MOUNT_POINT", tmp_path / "mnt" / "standby")
    mocker.patch.object(GrubControl, "GRUB_CFG_FILE", boot_dir / "grub" / "grub.cfg")
    mocker.patch.object(
        GrubControl, "CUSTOM_CFG_FILE", boot_dir / "grub" / "custom.cfg"
    )
    mocker.patch.object(GrubControl, "FSTAB_FILE", tmp_path / "etc" / "fstab")
    mocker.patch.object(GrubControl, "DEFAULT_GRUB_FILE", etc_dir / "default" / "grub")

    # patch OtaPartition
    mocker.patch.object(OtaPartition, "_get_root_device_file", return_value="/dev/sdx3")
    mocker.patch.object(OtaPartition, "_get_boot_device_file", return_value="/dev/sdx2")
    mocker.patch.object(
        OtaPartition, "_get_parent_device_file", return_value="/dev/sdx"
    )
    mocker.patch.object(
        OtaPartition, "_get_standby_device_file", return_value="/dev/sdx4"
    )

    # patch OtaPartitionFile
    mocker.patch.object(OtaPartitionFile, "_mount_cmd", return_value=0)

    # patch GrubControl
    def mock__get_uuid(dummy1, device):
        if device == "sdx3":
            return "01234567-0123-0123-0123-0123456789ab"
        if device == "sdx4":
            return "76543210-3210-3210-3210-ba9876543210"

    mocker.patch.object(GrubControl, "_get_uuid", mock__get_uuid)
    cmdline = "BOOT_IMAGE=/vmlinuz-5.4.0-73-generic root=UUID=01234567-0123-0123-0123-0123456789ab ro maybe-ubiquity"

    mocker.patch.object(GrubControl, "_get_cmdline", return_value=cmdline)
    reboot_mock = mocker.patch.object(GrubControl, "reboot", return_value=0)
    _grub_reboot_mock = mocker.patch.object(
        GrubControl, "_grub_reboot_cmd", return_value=0
    )
    # test start
    ota_client = OtaClient()

    with requests_mock.Mocker(real_http=True) as m:
        m.register_uri(
            "GET",
            "http://ota-server:8080/ota-server/data/usr/bin/kill",
            **error_injection,
        )
        ota_client.update(
            "123.x",
            "http://ota-server:8080/ota-server",
            json.dumps({"test": "my-cookie"}),
        )

    result, status = ota_client.status()
    assert result == OtaClientFailureType.NO_FAILURE
    assert status["status"] == "FAILURE"
    assert status["failure_type"] == "RECOVERABLE"
    failure_reason = status["failure_reason"]
    assert failure_reason.startswith(failure_reason_startswith)
    assert status["version"] == "a.b.c"
    assert status["failure_type"] == "RECOVERABLE"

    # make sure boot ota-partition is NOT switched
    assert os.readlink(boot_dir / "ota-partition") == "ota-partition.sdx3"
    assert not (boot_dir / "vmlinuz-ota.standby").exists()
    assert not (boot_dir / "initrd.img-ota.standby").exists()

    assert not (boot_dir / "ota-partition.sdx4" / "vmlinuz-ota").exists()
    assert not (boot_dir / "ota-partition.sdx4" / "initrd.img-ota").exists()
    assert open(boot_dir / "ota-partition.sdx4" / "status").read() == "FAILURE"
    assert open(boot_dir / "ota-partition.sdx4" / "version").read() == "123.x"
    # make sure grub.cfg is not created yet in standby boot partition
    assert not (boot_dir / "ota-partition.sdx4" / "grub.cfg").is_file()

    # custom.cfg is not created
    assert not (boot_dir / "grub" / "custom.cfg").exists()

    _grub_reboot_mock.assert_not_called()
    reboot_mock.assert_not_called()

    assert ota_client._ota_status.get_ota_status() == OtaStatus.FAILURE


def test_ota_client_update_with_initialize_boot_partition(mocker, tmp_path):
    from ota_client import OtaClient
    from ota_partition import OtaPartition, OtaPartitionFile
    from ota_status import OtaStatus
    from grub_control import GrubControl

    """
    tmp_path/boot
            /boot/grub/
            /boot/grub/grub.cfg
            /boot/grub/custom.cfg
            /etc/fstab
            /mnt/standby/
    /dev/sdx
    /dev/sdx2 /boot
    /dev/sdx3 / (UUID: 01234567-0123-0123-0123-0123456789ab)
    /dev/sdx4 (unmounted) (UUID: 76543210-3210-3210-3210-ba9876543210)
    """

    kernel_version = "5.4.0-73-generic"
    vmlinuz_file = f"vmlinuz-{kernel_version}"
    initrd_img_file = f"initrd.img-{kernel_version}"
    config_file = f"config-{kernel_version}"
    system_map_file = f"System.map-{kernel_version}"

    # directory setup
    boot_dir = tmp_path / "boot"
    boot_dir.mkdir()
    (boot_dir / vmlinuz_file).write_text(vmlinuz_file)
    (boot_dir / initrd_img_file).write_text(initrd_img_file)
    (boot_dir / config_file).write_text(config_file)
    (boot_dir / system_map_file).write_text(system_map_file)

    mount_dir = tmp_path / "mnt"
    mount_dir.mkdir()

    grub_dir = boot_dir / "grub"
    grub_dir.mkdir()
    grub_cfg = grub_dir / "grub.cfg"
    grub_cfg.write_text(grub_cfg_wo_submenu)

    etc_dir = tmp_path / "etc"
    etc_dir.mkdir()
    fstab = etc_dir / "fstab"
    fstab.write_text(FSTAB_DEV_DISK_BY_UUID)
    default_dir = etc_dir / "default"
    default_dir.mkdir()
    default_grub = default_dir / "grub"
    default_grub.write_text(DEFAULT_GRUB)

    # file path patch
    mocker.patch.object(OtaPartition, "BOOT_DIR", boot_dir)
    mocker.patch.object(OtaClient, "MOUNT_POINT", tmp_path / "mnt" / "standby")
    mocker.patch.object(GrubControl, "GRUB_CFG_FILE", boot_dir / "grub" / "grub.cfg")
    mocker.patch.object(
        GrubControl, "CUSTOM_CFG_FILE", boot_dir / "grub" / "custom.cfg"
    )
    mocker.patch.object(GrubControl, "FSTAB_FILE", tmp_path / "etc" / "fstab")
    mocker.patch.object(GrubControl, "DEFAULT_GRUB_FILE", etc_dir / "default" / "grub")

    # patch OtaPartition
    mocker.patch.object(OtaPartition, "_get_root_device_file", return_value="/dev/sdx3")
    mocker.patch.object(OtaPartition, "_get_boot_device_file", return_value="/dev/sdx2")
    mocker.patch.object(
        OtaPartition, "_get_parent_device_file", return_value="/dev/sdx"
    )
    mocker.patch.object(
        OtaPartition, "_get_standby_device_file", return_value="/dev/sdx4"
    )

    # patch OtaPartitionFile
    mocker.patch.object(OtaPartitionFile, "_mount_cmd", return_value=0)

    # patch GrubControl
    def mock__get_uuid(dummy1, device):
        if device == "sdx3":
            return "01234567-0123-0123-0123-0123456789ab"
        if device == "sdx4":
            return "76543210-3210-3210-3210-ba9876543210"

    mocker.patch.object(GrubControl, "_get_uuid", mock__get_uuid)
    cmdline = "BOOT_IMAGE=/vmlinuz-5.4.0-73-generic root=UUID=01234567-0123-0123-0123-0123456789ab ro maybe-ubiquity"

    mocker.patch.object(GrubControl, "_get_cmdline", return_value=cmdline)
    reboot_mock = mocker.patch.object(GrubControl, "reboot", return_value=0)
    _grub_reboot_mock = mocker.patch.object(
        GrubControl, "_grub_reboot_cmd", return_value=0
    )
    # NOTE:
    # basically patch to _count_menuentry is not required if
    # mock__grub_mkconfig_cmd is more sophisticated.
    mocker.patch.object(GrubControl, "_count_menuentry", return_value=1)

    def mock__grub_mkconfig_cmd(dummy1, outfile):
        # TODO: depend on the outfile, grub.cfg with vmlinuz-ota entry should be output.
        salt = " "  # to make the data different from grub_cfg_wo_submenu
        outfile.write_text(grub_cfg_wo_submenu + salt)

    mocker.patch.object(GrubControl, "_grub_mkconfig_cmd", mock__grub_mkconfig_cmd)

    # test start
    ota_client = OtaClient()

    # make sure grub.cfg is not created yet in standby boot partition
    assert not (boot_dir / "ota-partition.sdx4" / "grub.cfg").is_file()

    # changed from regular file to symlink file
    assert os.readlink(grub_cfg) == str(Path("..") / "ota-partition" / "grub.cfg")
    # grub.cfg is generated under ota-partition
    assert (
        open(boot_dir / "ota-partition" / "grub.cfg").read()
        == grub_cfg_wo_submenu + " "
    )
    assert (
        open(boot_dir / "ota-partition.sdx3" / "grub.cfg").read()
        == grub_cfg_wo_submenu + " "
    )

    ota_client.update(
        "123.x", "http://ota-server:8080/ota-server", json.dumps({"test": "my-cookie"})
    )

    # make sure boot ota-partition is NOT switched
    assert os.readlink(boot_dir / "ota-partition") == "ota-partition.sdx3"
    assert (
        os.readlink(boot_dir / "vmlinuz-ota.standby")
        == "ota-partition.sdx4/vmlinuz-ota"
    )
    assert (
        os.readlink(boot_dir / "initrd.img-ota.standby")
        == "ota-partition.sdx4/initrd.img-ota"
    )

    assert (
        os.readlink(boot_dir / "ota-partition.sdx4" / "vmlinuz-ota")
        == "vmlinuz-5.8.0-53-generic"  # FIXME
    )
    assert (
        os.readlink(boot_dir / "ota-partition.sdx4" / "initrd.img-ota")
        == "initrd.img-5.8.0-53-generic"  # FIXME
    )
    assert open(boot_dir / "ota-partition.sdx4" / "status").read() == "UPDATING"
    assert open(boot_dir / "ota-partition.sdx4" / "version").read() == "123.x"

    # custom.cfg is created
    assert (boot_dir / "grub" / "custom.cfg").is_file()
    assert open(boot_dir / "grub" / "custom.cfg").read() == custom_cfg

    # number of menuentry in grub_cfg_wo_submenu is 9
    _grub_reboot_mock.assert_called_once_with(9)
    reboot_mock.assert_called_once()

    # fstab
    assert (
        open(tmp_path / "mnt" / "standby" / "etc" / "fstab").read()
        == FSTAB_DEV_DISK_BY_UUID_STANDBY
    )
    assert ota_client._ota_status.get_ota_status() == OtaStatus.UPDATING


def test_ota_client_update_post_process(mocker, tmp_path):
    from ota_client import OtaClient
    from ota_partition import OtaPartition, OtaPartitionFile
    from ota_status import OtaStatus
    from grub_control import GrubControl

    """
    tmp_path/boot
            /boot/grub/
            /boot/grub/grub.cfg -> ../ota-partition/grub.cfg
            /boot/grub/custom.cfg
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
    (sdx4 / "status").write_text("UPDATING")

    mount_dir = tmp_path / "mnt"
    mount_dir.mkdir()

    grub_dir = boot_dir / "grub"
    grub_dir.mkdir()
    grub_cfg = grub_dir / "grub.cfg"
    grub_cfg.symlink_to(Path("..") / "ota-partition" / "grub.cfg")
    grub_cfg.write_text(grub_cfg_wo_submenu)

    etc_dir = tmp_path / "etc"
    etc_dir.mkdir()
    fstab = etc_dir / "fstab"
    fstab.write_text(FSTAB_DEV_DISK_BY_UUID)
    default_dir = etc_dir / "default"
    default_dir.mkdir()
    default_grub = default_dir / "grub"
    default_grub.write_text(DEFAULT_GRUB)

    # file path patch
    mocker.patch.object(OtaPartition, "BOOT_DIR", boot_dir)
    mocker.patch.object(OtaClient, "MOUNT_POINT", tmp_path / "mnt" / "standby")
    mocker.patch.object(GrubControl, "GRUB_CFG_FILE", boot_dir / "grub" / "grub.cfg")
    mocker.patch.object(
        GrubControl, "CUSTOM_CFG_FILE", boot_dir / "grub" / "custom.cfg"
    )
    mocker.patch.object(GrubControl, "FSTAB_FILE", tmp_path / "etc" / "fstab")
    mocker.patch.object(GrubControl, "DEFAULT_GRUB_FILE", etc_dir / "default" / "grub")

    # patch OtaPartition
    mocker.patch.object(OtaPartition, "_get_root_device_file", return_value="/dev/sdx4")
    mocker.patch.object(OtaPartition, "_get_boot_device_file", return_value="/dev/sdx2")
    mocker.patch.object(
        OtaPartition, "_get_parent_device_file", return_value="/dev/sdx"
    )
    mocker.patch.object(
        OtaPartition, "_get_standby_device_file", return_value="/dev/sdx3"
    )

    # patch OtaPartitionFile
    mocker.patch.object(OtaPartitionFile, "_mount_cmd", return_value=0)

    # patch GrubControl
    def mock__get_uuid(dummy1, device):
        if device == "sdx3":
            return "01234567-0123-0123-0123-0123456789ab"
        if device == "sdx4":
            return "76543210-3210-3210-3210-ba9876543210"

    mocker.patch.object(GrubControl, "_get_uuid", mock__get_uuid)
    cmdline = "BOOT_IMAGE=/vmlinuz-5.4.0-73-generic root=UUID=01234567-0123-0123-0123-0123456789ab ro maybe-ubiquity"

    mocker.patch.object(GrubControl, "_get_cmdline", return_value=cmdline)
    reboot_mock = mocker.patch.object(GrubControl, "reboot", return_value=0)
    _grub_reboot_mock = mocker.patch.object(
        GrubControl, "_grub_reboot_cmd", return_value=0
    )
    # NOTE:
    # basically patch to _count_menuentry is not required if
    # mock__grub_mkconfig_cmd is more sophisticated.
    mocker.patch.object(GrubControl, "_count_menuentry", return_value=1)

    def mock__grub_mkconfig_cmd(dummy1, outfile):
        # TODO: depend on the outfile, grub.cfg with vmlinuz-ota entry should be output.
        outfile.write_text(grub_cfg_wo_submenu)

    mocker.patch.object(GrubControl, "_grub_mkconfig_cmd", mock__grub_mkconfig_cmd)

    # test start
    ota_client = OtaClient()

    # make sure boot ota-partition is switched
    assert os.readlink(boot_dir / "ota-partition") == "ota-partition.sdx4"

    assert open(boot_dir / "ota-partition.sdx3" / "status").read() == "SUCCESS"
    assert ota_client._ota_status.get_ota_status() == OtaStatus.SUCCESS

    assert (  # NOTE: mock__grub_mkconfig_cmd returns grub_cfg_wo_submenu
        open(boot_dir / "ota-partition.sdx4" / "grub.cfg").read() == grub_cfg_wo_submenu
    )

    _grub_reboot_mock.assert_not_called()
    reboot_mock.assert_not_called()
    # TODO:
    # assert /etc/default/grub is updated
    # assert /boot/grub/grub.cfg is updated


PERSISTENTS_TXT = """\
'/etc/hosts'
'/etc/hostname'
'/etc/resolv.conf'
'/etc/netplan'
'/foo/bar'
"""


def test_ota_client__copy_persistent_files(mocker, tmp_path):
    from ota_client import OtaClient
    from ota_partition import OtaPartition, OtaPartitionFile
    from grub_control import GrubControl

    """
    tmp_path/etc/fstab
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
    (sdx4 / "status").write_text("INITIALIZED")

    # directory setup
    mount_dir = tmp_path / "mnt"
    mount_dir.mkdir()

    passwd_file = mount_dir / "etc" / "passwd"
    group_file = mount_dir / "etc" / "group"
    (mount_dir / "etc").mkdir()
    # copy /etc/passwd to mount_dir / "etc/passwd"
    shutil.copy("/etc/passwd", passwd_file)
    # copy /etc/group to mount_dir / "etc/group"
    shutil.copy("/etc/group", group_file)

    etc_dir = tmp_path / "etc"
    etc_dir.mkdir()

    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    persistents = tmp_dir / "persistents.txt"
    persistents.write_text(PERSISTENTS_TXT)

    # file path patch
    mocker.patch.object(OtaPartition, "BOOT_DIR", boot_dir)
    mocker.patch.object(OtaClient, "MOUNT_POINT", tmp_path / "mnt" / "standby")
    mocker.patch.object(GrubControl, "GRUB_CFG_FILE", boot_dir / "grub" / "grub.cfg")
    mocker.patch.object(
        GrubControl, "CUSTOM_CFG_FILE", boot_dir / "grub" / "custom.cfg"
    )
    mocker.patch.object(GrubControl, "FSTAB_FILE", tmp_path / "etc" / "fstab")
    mocker.patch.object(GrubControl, "DEFAULT_GRUB_FILE", etc_dir / "default" / "grub")

    # patch OtaPartition
    mocker.patch.object(OtaPartition, "_get_root_device_file", return_value="/dev/sdx3")
    mocker.patch.object(OtaPartition, "_get_boot_device_file", return_value="/dev/sdx2")
    mocker.patch.object(
        OtaPartition, "_get_parent_device_file", return_value="/dev/sdx"
    )
    mocker.patch.object(
        OtaPartition, "_get_standby_device_file", return_value="/dev/sdx4"
    )

    # patch OtaPartitionFile
    mocker.patch.object(OtaPartitionFile, "_mount_cmd", return_value=0)

    # test start
    ota_client = OtaClient()
    ota_client._copy_persistent_files(persistents, mount_dir)

    assert open("/etc/hostname").read() == open(mount_dir / "etc" / "hostname").read()
