import pytest
import pathlib

from e2e_tests.params_for_test import *


def _compare_files(path_l: pathlib.Path, path_r: pathlib.Path):
    """
    check whether files in the same relative path are the same
    """
    l_stat, r_stat = path_l.lstat(), path_r.lstat()

    # check the size is the same
    if l_stat.st_size != r_stat.st_size:
        return False
    # check the permission and ownership is the same
    if (
        l_stat.st_uid != r_stat.st_uid
        or l_stat.st_gid != r_stat.st_gid
        or l_stat.st_mode != r_stat.st_mode
    ):
        return False
    # (pass)check the file contents are the same
    return True


# TODO: REFACTORING REQUIRED! ALL TEST SHOULD BE BOUNDED TOGETHER!
# step1: apply the OTA update
def test_ota_update(ota_request, ota_client_service_instance):
    # check if ota is successful
    assert ota_client_service_instance._ota_update(ota_request)


# step2.1: test the bankB and fstab file
def test_bank_b(dir_list):
    bank_b = dir_list["BANKB_DIR"]
    ota_source = dir_list["OTA_SOURCE_DIR"]

    # walk through bank_b to check the files are correctly applied
    for entry_bank_b in bank_b.rglob("*"):
        entry_source = ota_source / entry_bank_b.relative_to(bank_b)
        # TODO: some files are generated during OTA update, should whitelist those files
        assert _compare_files(entry_bank_b, entry_source)


def test_fstab(fstab_file):
    with open(fstab_file) as f:
        assert f.read() == FSTAB_BY_UUID_BANKB


# step2.2: test custom grub files
def test_custom_grub(custom_cfg_file):
    with open(custom_cfg_file) as f:
        assert f.read() == GRUB_CUSTOM_CFG_BANKB


# step2.3: test ota status
def test_ota_status(dir_list):
    ota_status_file = dir_list["BOOT_DIR"] / "ota_status"
    assert ota_status_file.read_text() == UPDATED_OTA_STATUS


# step2.4: test updated ecuinfo
def test_updated_ecuinfo(ecuinfo_yaml_file):
    import yaml

    expected = yaml.safe_load(UPDATED_ECUINFO_YAML)
    with open(ecuinfo_yaml_file) as f:
        updated_ecuinfo = yaml.safe_load(f)

    assert updated_ecuinfo == expected
