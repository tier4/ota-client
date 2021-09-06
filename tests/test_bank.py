from pathlib import Path
import re
import pytest

BLKIDS = b'/dev/sda4: UUID="ad0cd79a-1752-47bb-9274-f9aa4e289cb9" TYPE="ext4" PARTUUID="3829c8d5-bb87-49fd-a0da-a557d9213276"\n/dev/sda3: UUID="3a1c99e7-46d9-41b1-8b0a-b07bceef1d02" TYPE="ext4" PARTUUID="8a80ed37-7870-414e-b276-53c7ca6f6836"\n/dev/sda1: PARTUUID="f511c6d9-e12c-4126-afb4-beec42587932"\n/dev/sda2: UUID="cc59073d-9e5b-41e1-b724-576259341132" TYPE="ext4" PARTUUID="efc328ac-be3a-4f4e-bc20-38b8629babc9"\n'
BLKID_SDA2 = b'/dev/sda2: UUID="cc59073d-9e5b-41e1-b724-576259341132" TYPE="ext4" PARTUUID="efc328ac-be3a-4f4e-bc20-38b8629babc9"\n'
BLKID_SDA3 = b'/dev/sda3: UUID="3a1c99e7-46d9-41b1-8b0a-b07bceef1d02" TYPE="ext4" PARTUUID="8a80ed37-7870-414e-b276-53c7ca6f6836"\n'
BLKID_SDA4 = b'/dev/sda4: UUID="ad0cd79a-1752-47bb-9274-f9aa4e289cb9" TYPE="ext4" PARTUUID="3829c8d5-bb87-49fd-a0da-a557d9213276"\n'

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

FSTAB_EFI_UUID = """\
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
UUID=B8F9-2BE3                            /boot/efi       vfat    umask=0077      0       1
/swapfile                                 none            swap    sw              0       0
"""


FSTAB_WO_ROOT = """\
# /etc/fstab: static file system information.
#
# Use 'blkid' to print the universally unique identifier for a
# device; this may be used with UUID= as a more robust way to name devices
# that works even if disks are added and removed. See fstab(5).
#
# <file system> <mount point>   <type>  <options>       <dump>  <pass>
# / was on /dev/nvme0n1p1 during installation
UUID=cc59073d-9e5b-41e1-b724-576259341132 /boot           ext4    errors=remount-ro 0       1
/swapfile                                 none            swap    sw              0       0
"""


FSTAB_EFI_WO_ROOT = """\
# /etc/fstab: static file system information.
#
# Use 'blkid' to print the universally unique identifier for a
# device; this may be used with UUID= as a more robust way to name devices
# that works even if disks are added and removed. See fstab(5).
#
# <file system> <mount point>   <type>  <options>       <dump>  <pass>
# / was on /dev/nvme0n1p1 during installation
UUID=cc59073d-9e5b-41e1-b724-576259341132 /boot           ext4    errors=remount-ro 0       1
UUID=B8F9-2BE3                            /boot/efi       vfat    umask=0077      0       1
/swapfile                                 none            swap    sw              0       0
"""


def mock__blkid_command(device=None):
    if device is None:
        return BLKIDS
    elif device == "/dev/sda2":
        return BLKID_SDA2
    elif device == "/dev/sda3":
        return BLKID_SDA3
    elif device == "/dev/sda4":
        return BLKID_SDA4


def test__get_ext4_blks_1(mocker):
    import bank

    mocker.patch("bank._blkid_command", mock__blkid_command)
    blks = bank._get_ext4_blks()
    assert blks == [
        {"DEV": "/dev/sda4", "UUID": "ad0cd79a-1752-47bb-9274-f9aa4e289cb9"},
        {"DEV": "/dev/sda3", "UUID": "3a1c99e7-46d9-41b1-8b0a-b07bceef1d02"},
        {"DEV": "/dev/sda2", "UUID": "cc59073d-9e5b-41e1-b724-576259341132"},
    ]


def test__get_ext4_blks_2(mocker):
    import bank

    mocker.patch("bank._blkid_command", mock__blkid_command)
    device = "/dev/sda3"
    blks = bank._get_ext4_blks(device)
    assert blks == [
        {"DEV": "/dev/sda3", "UUID": "3a1c99e7-46d9-41b1-8b0a-b07bceef1d02"}
    ]


def test__get_uuid_from_blkid_1(mocker):
    import bank

    mocker.patch("bank._blkid_command", mock__blkid_command)
    uuid = bank._get_uuid_from_blkid("/dev/sda3")
    assert uuid == "3a1c99e7-46d9-41b1-8b0a-b07bceef1d02"


def test__get_uuid_from_blkid_2(mocker):
    import bank

    mocker.patch("bank._blkid_command", mock__blkid_command)
    uuid = bank._get_uuid_from_blkid("/dev/sda4")
    assert uuid == "ad0cd79a-1752-47bb-9274-f9aa4e289cb9"


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
        (
            FSTAB_EFI_UUID,
            "ad0cd79a-1752-47bb-9274-f9aa4e289cb9",
            "cc59073d-9e5b-41e1-b724-576259341132",
        ),
    ],
)
def test__get_current_devfile_by_fstab(
    mocker, tmp_path: Path, fstab, root_uuid_exp, boot_uuid_exp
):
    import bank

    fstab_file = tmp_path / "fstab"
    fstab_file.write_text(fstab)

    _, root_uuid, _, boot_uuid = bank._get_current_devfile_by_fstab(fstab_file)
    assert root_uuid == root_uuid_exp
    assert boot_uuid == boot_uuid_exp


def test__get_current_devfile_by_fstab_with_exception_1(tmp_path: Path):
    import bank

    fstab_file = tmp_path / "fstab"
    fstab_file.write_text(FSTAB_WO_ROOT)

    with pytest.raises(UnboundLocalError):
        bank._get_current_devfile_by_fstab(fstab_file)


def test__get_current_devfile_by_fstab_with_exception_2(tmp_path: Path):
    import bank

    fstab_file = tmp_path / "fstab"
    fstab_file.write_text(FSTAB_EFI_WO_ROOT)

    with pytest.raises(UnboundLocalError):
        bank._get_current_devfile_by_fstab(fstab_file)


def test__get_bank_info(tmp_path: Path):
    import bank

    bankinfo_file = tmp_path / "bankinfo"
    BANKINFO = """\
banka: /dev/sda3
bankb: /dev/sda4
"""

    bankinfo_file.write_text(BANKINFO)

    banka, bankb = bank._get_bank_info(bankinfo_file)
    assert banka == "/dev/sda3"
    assert bankb == "/dev/sda4"


def test__get_bank_info_with_exception(tmp_path: Path):
    import bank

    bankinfo_file = tmp_path / "bankinfo"
    BANKINFO = """\
banka: /dev/sda3
bankb: /dev/sda4
"""

    # bankinfo_file.write_text(BANKINFO)

    banka, bankb = bank._get_bank_info(bankinfo_file)
    assert banka == ""
    assert bankb == ""


def test__gen_bankinfo_file(mocker, tmp_path: Path):
    import bank

    def mock__get_current_devfile_by_fstab(fstab_file):
        return (
            "/dev/sda3",
            "3a1c99e7-46d9-41b1-8b0a-b07bceef1d02",
            "/dev/sda2",
            "cc59073d-9e5b-41e1-b724-576259341132",
        )

    fstab_file = tmp_path / "fstab"
    fstab_file.write_text(FSTAB_EFI_UUID)

    mocker.patch(
        "bank._get_current_devfile_by_fstab", mock__get_current_devfile_by_fstab
    )
    mocker.patch("bank._blkid_command", mock__blkid_command)
    bankinfo_file = tmp_path / "bankinfo"
    assert bank._gen_bankinfo_file(bankinfo_file, fstab_file)
    banka, bankb = bank._get_bank_info(bankinfo_file)
    assert banka == "/dev/sda3"
    assert bankb == "/dev/sda4"


def test_BankInfo___init__(mocker, tmp_path: Path):
    import bank

    fstab_file = tmp_path / "fstab"
    fstab_file.write_text(FSTAB_EFI_UUID)

    def mock__gen_bankinfo_file(bank_info_file, fstab_file):
        return

    def mock__get_bank_info(bank_info_file):
        return "/dev/sda3", "/dev/sda4"

    def mock__get_uuid_from_blkid(bank):
        if bank == "/dev/sda3":
            return "3a1c99e7-46d9-41b1-8b0a-b07bceef1d02"
        elif bank == "/dev/sda4":
            return "ad0cd79a-1752-47bb-9274-f9aa4e289cb9"
        else:
            return ""

    mocker.patch("bank._BaseBankInfo._fstab_file", fstab_file)
    mocker.patch("bank._gen_bankinfo_file", mock__gen_bankinfo_file)
    mocker.patch("bank._get_bank_info", mock__get_bank_info)
    mocker.patch("bank._get_uuid_from_blkid", mock__get_uuid_from_blkid)
    bankinfo = bank.BankInfo()
    assert bankinfo.get_banka() == "/dev/sda3"
    assert bankinfo.get_bankb() == "/dev/sda4"
    assert bankinfo.get_banka_uuid() == "3a1c99e7-46d9-41b1-8b0a-b07bceef1d02"
    assert bankinfo.get_bankb_uuid() == "ad0cd79a-1752-47bb-9274-f9aa4e289cb9"
    assert bankinfo.is_banka("/dev/sda3")
    assert bankinfo.is_bankb("/dev/sda4")
    assert bankinfo.is_banka_uuid("3a1c99e7-46d9-41b1-8b0a-b07bceef1d02")
    assert bankinfo.is_bankb_uuid("ad0cd79a-1752-47bb-9274-f9aa4e289cb9")
    assert bankinfo.get_banka() == "/dev/sda3"
    assert bankinfo.get_bankb() == "/dev/sda4"
    assert bankinfo.get_banka_uuid() == "3a1c99e7-46d9-41b1-8b0a-b07bceef1d02"
    assert bankinfo.get_bankb_uuid() == "ad0cd79a-1752-47bb-9274-f9aa4e289cb9"
    assert bankinfo.get_current_bank() == "/dev/sda4"
    assert bankinfo.get_next_bank() == "/dev/sda3"
    assert bankinfo.get_current_bank_uuid() == "ad0cd79a-1752-47bb-9274-f9aa4e289cb9"
    assert bankinfo.get_next_bank_uuid() == "3a1c99e7-46d9-41b1-8b0a-b07bceef1d02"
    assert (
        bankinfo.get_current_bank_uuid_str()
        == "UUID=ad0cd79a-1752-47bb-9274-f9aa4e289cb9"
    )
    assert (
        bankinfo.get_next_bank_uuid_str() == "UUID=3a1c99e7-46d9-41b1-8b0a-b07bceef1d02"
    )
    assert bankinfo.is_current_bank("/dev/sda4")
    assert not bankinfo.is_current_bank("/dev/sda3")
