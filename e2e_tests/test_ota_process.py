import pytest
import pathlib

# TODO: set up a local http server using pytest-xprocess

def _compare_files(path_l: pathlib.Path, path_r: pathlib.Path):
    """
    check whether files in the same relative path are the same
    """
    l_stat, r_stat = path_l.lstat(), path_r.lstat()
    
    # check the size is the same
    if l_stat.st_size != r_stat.st_size:
        return False
    # check the permission and ownership is the same
    if( l_stat.st_uid != r_stat.st_uid or 
        l_stat.st_gid != r_stat.st_gid or
        l_stat.st_mode != r_stat.st_mode):
        return False
    # (pass)check the file contents are the same
    return True

# step1: apply the OTA update
def test_ota_update(
    ota_request,
    ota_client_service_instance):
    # start the OTA process
    ota_result = ota_client_service_instance._ota_update(ota_request)

    # check if ota is successful
    assert ota_result

# step2.1: test the bankB
def test_bank_b(configs):
    bank_b = configs["BANKB_DIR"]
    ota_source = configs["OTA_SOURCE_DIR"]
    
    # walk through bank_b to check the files are correctly applied
    for entry_bank_b in bank_b.rglob("*"):
        entry_source = ota_source / entry_bank_b.relative_to(bank_b)
        assert _compare_files(entry_bank_b, entry_source)

# step2.2: TODO: test grub files
def test_grub(
    tmp_path_factory: pathlib.Path,
    configs,):
    pass

# step2.3: test ota status
def test_ota_status(configs):
    ota_status_file = configs["BOOT_DIR"] / "ota_status"
    assert ota_status_file.read_text() == "SWITCHB"

# step2.4: test updated ecuinfo
def test_updated_ecuinfo(configs):
    # expected
    """
    main_ecu:
        ecu_name: 'autoware_ecu'
        ecu_type: 'autoware'
        ecu_id: '1'
        version: '0.5.1'
        independent: True
    """
    import ota_client

    rd_ecuinfo = ota_client._read_ecu_info(str(configs["OTA_DIR"] / "ecuinfo.yaml"))
    assert rd_ecuinfo["main_ecu"]["ecu_name"] == "autoware_ecu"
    assert rd_ecuinfo["main_ecu"]["ecu_type"] == "autoware"
    assert rd_ecuinfo["main_ecu"]["ecu_id"] == "1"
    assert rd_ecuinfo["main_ecu"]["version"] == "0.5.1"
    assert rd_ecuinfo["main_ecu"]["independent"] == True

