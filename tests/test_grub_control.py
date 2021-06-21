import os
import pytest


def test_grub_ctl_grub_configuration(tmpdir):
    from app.grub_control import GrubCtl

    default_grub_file = tmpdir.join("grub")
    grub = """\
GRUB_DEFAULT=0
GRUB_TIMEOUT_STYLE=hidden
GRUB_TIMEOUT=0
GRUB_DISTRIBUTOR=`lsb_release -i -s 2> /dev/null || echo Debian`
GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"
GRUB_CMDLINE_LINUX=""
"""
    default_grub_file.write(grub)

    grub_ctl = GrubCtl(default_grub_file=default_grub_file)
    r = grub_ctl.grub_configuration()
    assert r

    grub_exp = """\
GRUB_DEFAULT=0
GRUB_TIMEOUT_STYLE=menu
GRUB_TIMEOUT=10
GRUB_DISTRIBUTOR=`lsb_release -i -s 2> /dev/null || echo Debian`
GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"
GRUB_CMDLINE_LINUX=""
"""
    assert default_grub_file.read() == grub_exp
