import os
import pytest

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

FSTAB_DEV_DISK_BY_UUID_W_LOG = """\
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
LABEL=LOG /media/autoware/LOG ext4 nofail 0 0
"""

FSTAB_TO_BE_MERGED = """\
# UNCONFIGURED FSTAB FOR BASE SYSTEM
tmpfs /media/autoware/ROSBAG tmpfs rw,nosuid,nodev,noexec,nofail,size=10G,mode=1755 0 0
LABEL=LOG /media/autoware/LOG ext4 nofail 0 0
"""

FSTAB_MERGED = """\
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
tmpfs /media/autoware/ROSBAG tmpfs rw,nosuid,nodev,noexec,nofail,size=10G,mode=1755 0 0
LABEL=LOG /media/autoware/LOG ext4 nofail 0 0
"""

FSTAB_MERGED_W_LOG = """\
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
LABEL=LOG /media/autoware/LOG ext4 nofail 0 0
tmpfs /media/autoware/ROSBAG tmpfs rw,nosuid,nodev,noexec,nofail,size=10G,mode=1755 0 0
"""


@pytest.mark.parametrize(
    "fstab_active, fstab_standby, fstab_merged",
    [
        (FSTAB_DEV_DISK_BY_UUID, FSTAB_TO_BE_MERGED, FSTAB_MERGED),
        (FSTAB_DEV_DISK_BY_UUID_W_LOG, FSTAB_TO_BE_MERGED, FSTAB_MERGED_W_LOG),
    ],
)
def test_grub_control_update_fstab(
    mocker, tmp_path, fstab_active, fstab_standby, fstab_merged
):
    from grub_control import GrubControl

    mount_dir = tmp_path / "mnt"
    mount_dir.mkdir()
    mount_etc_dir = mount_dir / "etc"
    mount_etc_dir.mkdir()
    mount_etc_fstab = mount_etc_dir / "fstab"
    mount_etc_fstab.write_text(fstab_standby)

    etc_dir = tmp_path / "etc"
    etc_dir.mkdir()
    fstab = etc_dir / "fstab"
    fstab.write_text(fstab_active)

    # patch GrubControl
    def mock__get_uuid(dummy1, device):
        if device == "sdx3":
            return "01234567-0123-0123-0123-0123456789ab"
        if device == "sdx4":
            return "76543210-3210-3210-3210-ba9876543210"

    mocker.patch.object(GrubControl, "_get_uuid", mock__get_uuid)
    mocker.patch.object(GrubControl, "FSTAB_FILE", tmp_path / "etc" / "fstab")

    grub_control = GrubControl()
    grub_control.update_fstab(mount_dir, "sdx3", "sdx4")

    assert open(mount_etc_fstab).read() == fstab_merged
