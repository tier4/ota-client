import pytest
import pytest_mock
import pathlib

from bank import BankInfo

from ota_client import OtaClient
from ota_client_service import OtaClientService

# TODO: set up a local http server using pytest-xprocess

def _compare_files(path_l: pathlib.Path, path_r: pathlib.Path):
    l_stat, r_stat = path_l.lstat(), path_r.lstat()
    
    # check the size is the same
    if l_stat.st_size != r_stat.st_size:
        return False
    # check the permission and ownership is the same
    if( l_stat.st_uid != r_stat.st_uid or 
        l_stat.st_gid != r_stat.st_gid or
        l_stat.st_mode != r_stat.st_mode
        ):
        return False
    # (by pass)check the file contents are the same
    return True

# test the OTA process
def test_ota_update(
    mocker: pytest_mock.MockerFixture,
    tmp_path_factory: pathlib.Path,
    ota_request,
    ota_client_service_instance: OtaClientService):
    # start the OTA process
    assert ota_client_service_instance._ota_update(ota_request)

# test the bankB
def test_bank_b(
    tmp_path_factory: pathlib.Path,
    configs,
):
    bank_b = tmp_path_factory / configs["BANKB_DIR"]
    ota_source = configs["OTA_SOURCE_DIR"]
    
    # walk through bank_b to check the files are correctly applied
    for entry_bank_b in bank_b.rglob("*"):
        entry_source = ota_source / entry_bank_b.relative_to(bank_b)
        assert _compare_files(entry_bank_b, entry_source)

# TODO: test grub files
def test_grub(
    tmp_path_factory: pathlib.Path,
    configs,):
    pass

# TODO: test ota_status
def test_ota_status(
    tmp_path_factory: pathlib.Path,
    configs,
):
    ota_status_file: pathlib.Path = tmp_path_factory / configs["BOOT_DIR"] / "ota_status"
    assert ota_status_file.read_text() == "SWITCHB"