import os
import pytest


@pytest.fixture
def bankinfo_file(tmpdir):
    bank = """\
banka: /dev/sda3
bankb: /dev/sda4
"""
    bankinfo = tmpdir.join("bankinfo.yaml")
    bankinfo.write(bank)
    return bankinfo


@pytest.fixture
def grub_file_default(tmpdir):
    grub = """\
GRUB_DEFAULT=0
GRUB_TIMEOUT_STYLE=hidden
GRUB_TIMEOUT=0
GRUB_DISTRIBUTOR=`lsb_release -i -s 2> /dev/null || echo Debian`
GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"
GRUB_CMDLINE_LINUX=""
"""
    grub_file = tmpdir.join("grub")
    grub_file.write(grub)
    return grub_file


@pytest.fixture
def uuid_a():
    return "01234567-0123-0123-0123-0123456789ab"


@pytest.fixture
def uuid_b():
    return "76543210-3210-3210-3210-ba9876543210"


@pytest.fixture
def custom_cfg_file(tmpdir, uuid_a):
    cfg = f"""
menuentry 'GNU/Linux' {{
        linux   /vmlinuz-5.4.0-74-generic root=UUID={uuid_a} ro  quiet splash $vt_handoff
        initrd  /initrd.img-5.4.0-74-generic
}}"""
    custom_cfg = tmpdir.join("custom.cfg")
    custom_cfg.write(cfg)
    return custom_cfg


@pytest.fixture
def grub_ctl_instance(tmpdir, mocker, uuid_a, uuid_b, bankinfo_file, custom_cfg_file):
    from grub_control import GrubCtl
    from bank import BankInfo

    def mock_get_uuid_from_blkid(bank):
        if bank == "/dev/sda3":
            return uuid_a
        if bank == "/dev/sda4":
            return uuid_b

    def mock_get_current_bank_uuid(_):
        return uuid_a

    def mock_get_next_bank_uuid(_):
        return uuid_b

    mocker.patch("bank._get_uuid_from_blkid", mock_get_uuid_from_blkid)
    mocker.patch.object(BankInfo, "get_current_bank_uuid", mock_get_current_bank_uuid)
    mocker.patch.object(BankInfo, "get_next_bank_uuid", mock_get_next_bank_uuid)
    grub_ctl = GrubCtl(bank_info_file=bankinfo_file)
    return grub_ctl


def test_grub_ctl_grub_configuration(tmpdir, grub_file_default):
    from grub_control import GrubCtl

    grub_ctl = GrubCtl(default_grub_file=grub_file_default)
    r = grub_ctl.grub_configuration()
    assert r

    grub_exp = """\
GRUB_DEFAULT=0
GRUB_TIMEOUT_STYLE=menu
GRUB_TIMEOUT=10
GRUB_DISTRIBUTOR=`lsb_release -i -s 2> /dev/null || echo Debian`
GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"
GRUB_CMDLINE_LINUX=""
GRUB_DISABLE_SUBMENU=y
"""
    assert grub_file_default.read() == grub_exp


grub_ctl_change_to_next_bank_params = [
    (
        None,
        None,
        f"""
menuentry 'GNU/Linux' {{
        linux   /vmlinuz-5.4.0-74-generic root=UUID=76543210-3210-3210-3210-ba9876543210 ro  quiet splash $vt_handoff
        initrd  /initrd.img-5.4.0-74-generic
}}""",
    ),
    (
        "/boot/vmlinuz-1.2.3-45-generic",  # linux image
        "/boot/initrd.img-1.2.3-45-generic",  # initrd image
        f"""
menuentry 'GNU/Linux' {{
        linux   /vmlinuz-1.2.3-45-generic root=UUID=76543210-3210-3210-3210-ba9876543210 ro  quiet splash $vt_handoff
        initrd  /initrd.img-1.2.3-45-generic
}}""",
    ),
]


@pytest.mark.parametrize(
    "vmlinuz, initrd, expect",
    grub_ctl_change_to_next_bank_params,
)
def test_grub_ctl_change_to_next_bank(
    grub_ctl_instance, uuid_b, custom_cfg_file, vmlinuz, initrd, expect
):
    grub_ctl_instance.change_to_next_bank(custom_cfg_file, vmlinuz, initrd)

    assert custom_cfg_file.read() == expect
