import os
import pytest
from pathlib import Path
from pprint import pprint

from tests.grub_cfg_params import (
    grub_cfg_params,
    grub_cfg_custom_cfg_params,
    grub_cfg_wo_submenu,
    UUID_A,
    UUID_B,
)

@pytest.fixture
def bankinfo_file(tmp_path: Path):
    bank = """\
banka: /dev/sda3
bankb: /dev/sda4
"""
    bankinfo = tmp_path / "bankinfo.yaml"
    bankinfo.write_text(bank)
    return bankinfo


@pytest.fixture
def grub_file_default(tmp_path: Path):
    grub = """\
GRUB_DEFAULT=0
GRUB_TIMEOUT_STYLE=hidden
GRUB_TIMEOUT=0
GRUB_DISTRIBUTOR=`lsb_release -i -s 2> /dev/null || echo Debian`
GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"
GRUB_CMDLINE_LINUX=""
"""
    grub_file = tmp_path / "grub"
    grub_file.write_text(grub)
    return grub_file


@pytest.fixture
def custom_cfg_file(tmp_path: Path):
    cfg = f"""
menuentry 'GNU/Linux' {{
        linux   /vmlinuz-5.4.0-74-generic root=UUID={UUID_A} ro  quiet splash $vt_handoff
        initrd  /initrd.img-5.4.0-74-generic
}}"""
    custom_cfg = tmp_path / "custom.cfg"
    custom_cfg.write_text(cfg)
    return custom_cfg


@pytest.fixture
def grub_ctl_instance(tmp_path: Path, mocker, bankinfo_file, custom_cfg_file):
    import bank
    import grub_control

    def mock_get_uuid_from_blkid(bank):
        if bank == "/dev/sda3":
            return UUID_A
        if bank == "/dev/sda4":
            return UUID_B

    def mock_get_current_bank_uuid(_):
        return UUID_A

    def mock_get_next_bank_uuid(_):
        return UUID_B

    def mock_get_current_bank(_):
        return "/dev/sda3"

    def mock_get_next_bank(_):
        return "/dev/sda4"

    def mock_make_grub_configuration_file(output_file):
        with open(output_file, mode="w") as f:
            f.write(grub_cfg_wo_submenu)

    mocker.patch.object(bank._baseBankInfo, "_bank_info_file", bankinfo_file)
    mocker.patch.object(grub_control.GrubCtl, "_custom_cfg_file", custom_cfg_file)

    mocker.patch.object(bank, "_get_uuid_from_blkid", mock_get_uuid_from_blkid)
    mocker.patch.object(bank.BankInfo, "get_current_bank_uuid", mock_get_current_bank_uuid)
    mocker.patch.object(bank.BankInfo, "get_next_bank_uuid", mock_get_next_bank_uuid)
    mocker.patch.object(bank.BankInfo, "get_current_bank", mock_get_current_bank)
    mocker.patch.object(bank.BankInfo, "get_next_bank", mock_get_next_bank)
    mocker.patch.object(bank.BankInfo, "_setup_current_next_root_dev", return_value=True)
    mocker.patch.object(
        grub_control, "_make_grub_configuration_file", mock_make_grub_configuration_file
    )
    grub_ctl = grub_control.GrubCtl()
    return grub_ctl


def test_grub_ctl_grub_configuration(mocker, tmp_path: Path, grub_file_default: Path):
    import grub_control
    import bank

    mocker.patch.object(grub_control.GrubCtl, "_default_grub_file", grub_file_default)
    mocker.patch.object(bank, "_get_current_devfile_by_fstab", return_value=("", "", "", ""))
    grub_ctl = grub_control.GrubCtl()
    r = grub_ctl._grub_configuration()
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
    assert grub_file_default.read_text() == grub_exp


grub_ctl_change_to_next_bank_params = [
    (
        None,
        None,
        f"""
menuentry 'GNU/Linux' {{
        linux   /vmlinuz-5.4.0-74-generic root=UUID={UUID_B} ro  quiet splash $vt_handoff
        initrd  /initrd.img-5.4.0-74-generic
}}""",
    ),
    (
        "/boot/vmlinuz-1.2.3-45-generic",  # linux image
        "/boot/initrd.img-1.2.3-45-generic",  # initrd image
        f"""
menuentry 'GNU/Linux' {{
        linux   /vmlinuz-1.2.3-45-generic root=UUID={UUID_B} ro  quiet splash $vt_handoff
        initrd  /initrd.img-1.2.3-45-generic
}}""",
    ),
]


@pytest.mark.parametrize(
    "vmlinuz, initrd, expect",
    grub_ctl_change_to_next_bank_params,
)
def test_grub_ctl_change_to_next_bank(
    grub_ctl_instance, custom_cfg_file: Path, vmlinuz, initrd, expect
):
    grub_ctl_instance.change_to_next_bank(custom_cfg_file, vmlinuz, initrd)

    assert custom_cfg_file.read_text() == expect


def test_find_custom_cfg_entry_from_grub_cfg(grub_ctl_instance):
    index = grub_ctl_instance._find_custom_cfg_entry_from_grub_cfg()
    assert index == 0


@pytest.mark.parametrize(
    "grub_cfg, expect", grub_cfg_params, ids=[p[0]["id"] for p in grub_cfg_params]
)
def test_grub_cfg_parser(grub_cfg, expect):
    from grub_control import GrubCfgParser

    parser = GrubCfgParser(grub_cfg["grub_cfg"])
    assert parser.parse() == expect


@pytest.mark.parametrize(
    "grub_cfg, custom_cfg, vmlinuz, initrd",
    grub_cfg_custom_cfg_params,
    ids=[p[0]["id"] for p in grub_cfg_custom_cfg_params],
)
def test_make_grub_custom_configuration_file(
    mocker, grub_cfg: Path, custom_cfg: Path, vmlinuz, initrd, grub_ctl_instance, tmp_path: Path
):
    grub = tmp_path / "grub.cfg"
    grub.write_text(grub_cfg["grub_cfg"])
    custom = tmp_path / "custom.cfg"

    mocker.patch("platform.release", return_value="5.4.0-73-generic")
    # grub_ctl_instance has UUID_A as current_bank_uuid
    # search "5.4.0-73-generic" and UUID_A(=0123...)
    # and replace UUID with UUID_B(=7654...),
    # vmlinuz-xxx with vmlinuz by param,
    # initrd.img-xxx with initrd by param.
    assert grub_ctl_instance.make_grub_custom_configuration_file(
        grub, custom, vmlinuz, initrd
    )
    custom_out = custom.read_text()
    assert custom_out == custom_cfg
