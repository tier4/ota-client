import re
import pytest

BLKIDS = b'/dev/sda4: UUID="ad0cd79a-1752-47bb-9274-f9aa4e289cb9" TYPE="ext4" PARTUUID="3829c8d5-bb87-49fd-a0da-a557d9213276"\n/dev/sda3: UUID="3a1c99e7-46d9-41b1-8b0a-b07bceef1d02" TYPE="ext4" PARTUUID="8a80ed37-7870-414e-b276-53c7ca6f6836"\n/dev/sda1: PARTUUID="f511c6d9-e12c-4126-afb4-beec42587932"\n/dev/sda2: UUID="cc59073d-9e5b-41e1-b724-576259341132" TYPE="ext4" PARTUUID="efc328ac-be3a-4f4e-bc20-38b8629babc9"\n'

FSTAB_DEV_DISK_BY_UUID = """\
# /etc/fstab: static file system information.
#
# Use 'blkid' to print the universally unique identifier for a
# device; this may be used with UUID= as a more robust way to name devices
# that works even if disks are added and removed. See fstab(5).
#
# <file system> <mount point>   <type>  <options>       <dump>  <pass>
# / was on /dev/sda3 during curtin installation
/dev/disk/by-uuid/ad0cd79a-1752-47bb-9274-f9aa4e289cb9 / ext4 defaults 0 0
# /boot was on /dev/sda2 during curtin installation
/dev/disk/by-uuid/cc59073d-9e5b-41e1-b724-576259341132 /boot ext4 defaults 0 0
/swap.img	none	swap	sw	0	0
"""

FSTAB_UUID = """\
# /etc/fstab: static file system information.
#
# Use 'blkid' to print the universally unique identifier for a
# device; this may be used with UUID= as a more robust way to name devices
# that works even if disks are added and removed. See fstab(5).
#
# <file system> <mount point>   <type>  <options>       <dump>  <pass>
# / was on /dev/nvme0n1p1 during installation
UUID=ad0cd79a-1752-47bb-9274-f9aa4e289cb9 /               ext4    errors=remount-ro 0       1
UUID=cc59073d-9e5b-41e1-b724-576259341132 /boot           ext4    errors=remount-ro 0       1
/swapfile                                 none            swap    sw              0       0
"""


def test__get_ext4_blks(mocker):
    import bank

    def mock__blkid_command(device=None):
        if device is None:
            return BLKIDS

    mocker.patch("bank._blkid_command", mock__blkid_command)
    blks = bank._get_ext4_blks()
    assert blks == [
        {"DEV": "/dev/sda4", "UUID": "ad0cd79a-1752-47bb-9274-f9aa4e289cb9"},
        {"DEV": "/dev/sda3", "UUID": "3a1c99e7-46d9-41b1-8b0a-b07bceef1d02"},
        {"DEV": "/dev/sda2", "UUID": "cc59073d-9e5b-41e1-b724-576259341132"},
    ]


def test__get_uuid_from_blkid(mocker):
    import bank

    def mock__blkid_command(device=None):
        return b"""\
/dev/sda3: UUID="3a1c99e7-46d9-41b1-8b0a-b07bceef1d02" TYPE="ext4" PARTUUID="8a80ed37-7870-414e-b276-53c7ca6f6836"
        """

    mocker.patch("bank._blkid_command", mock__blkid_command)
    uuid = bank._get_uuid_from_blkid("/dev/sda3")
    assert uuid == "3a1c99e7-46d9-41b1-8b0a-b07bceef1d02"


@pytest.mark.parametrize(
    "fstab, root_uuid_exp, boot_uuid_exp",
    [
        (
            FSTAB_DEV_DISK_BY_UUID,
            "ad0cd79a-1752-47bb-9274-f9aa4e289cb9",
            "cc59073d-9e5b-41e1-b724-576259341132",
        ),
        (
            FSTAB_UUID,
            "ad0cd79a-1752-47bb-9274-f9aa4e289cb9",
            "cc59073d-9e5b-41e1-b724-576259341132",
        ),
    ],
)
def test__get_current_devfile_by_fstab(
    mocker, tmp_path, fstab, root_uuid_exp, boot_uuid_exp
):
    import bank

    mocker.patch("bank.os.path.realpath", return_value="realpath")
    fstab_file = tmp_path / "fstab"
    fstab_file.write_text(fstab)

    (
        root_devfile,
        root_uuid,
        boot_devfile,
        boot_uuid,
    ) = bank._get_current_devfile_by_fstab(fstab_file)
    assert root_uuid == root_uuid_exp
    assert boot_uuid == boot_uuid_exp
