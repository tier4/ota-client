import pytest
import pytest_mock
import pathlib
import otaclient_pb2

from bank import BankInfo
from ota_client import OtaClient
from ota_client_service import OtaClientService

# initial status const
OTA_STATUS = "NORMAL"
BANK_INFO = """\
banka: /dev/sda3
bankb: /dev/sda4
"""
ECUID = "1\n"
ECUINFO_YAML = """\
main_ecu:
  ecu_name: 'autoware_ecu' 
  ecu_type: 'autoware'
  ecu_id: '1'
  version: '0.0.0'
  independent: True
  ip_addr: ''
"""

# TODO: find a way to define the working_dir
@pytest.fixture(scope="session", autouse=True)
def configs(
    tmp_path_factory: pathlib.Path,
    working_dir: str,
    ):
    return {
        "OTA_SOURCE_DIR": pathlib.Path(working_dir) / "./data",
        "BOOT_DIR": tmp_path_factory / "boot/ota",
        "BANKA_DIR": pathlib.Path(working_dir) / "./banka",
        "BANKB_DIR": pathlib.Path(working_dir) / "./bankb",
    }

# prepare files needed for the OTA
@pytest.fixture(scope="session")
def ota_client_init(tmp_path_factory: pathlib.Path):
    '''
    prepare ota_status file, bank_info_file, 
    ecuid_file and ecuinfo_yaml_file
    '''
    ota_d = tmp_path_factory / "boot" / "ota"
    ota_status_file = ota_d / "ota_status"
    bank_info_file = ota_d / "bankinfo.yaml"
    ecuid_file = ota_d / "ecuid"
    ecuinfo_yaml_file = ota_d / "ecuinfo.yaml"
    
    ota_status_file.write_text(OTA_STATUS)
    bank_info_file.write_text(BANK_INFO)
    ecuid_file.write_text(ECUID)
    ecuinfo_yaml_file.write_text(ECUINFO_YAML)
    return

# generate a ota_client fixture for testing
@pytest.fixture(scope="session")
def ota_client_instance(mocker, tmp_path_factory: pathlib.Path, ota_client_init):
    ota_d = tmp_path_factory / "boot" / "ota"

    return OtaClient(
        boot_status="NORMAL_BOOT",
        ota_status_file=str(ota_d / "ota_status"),
        bank_info_file=str(ota_d / "bankinfo.yaml"),
        ecuid_file=str(ota_d / "ecuid"),
        ecuinfo_yaml_file=str(ota_d / "ecuinfo.yaml"),
    )

# generate a ota_client_service fixture for testing
# TODO: mock the grpc server part
# TODO: mock the dst of bankB and bankA
@pytest.fixture(scope="session", autouse=True)
def ota_client_service_instance(
    ota_client_init,
    ota_client_instance: OtaClient):
    return OtaClientService(ota_client_instance)

# generate ota request
# TODO: ota request
@pytest.fixture(scope="session")
def ota_request():
    update_req = otaclient_pb2.OtaUpdateRequest()
    update_req.version = "0.0.1"
    eui = update_req.ecu_update_info.add()
    eui.ecu_info.ecu_name = "AutowareECU"
    eui.ecu_info.ecu_type = "autoware"
    eui.ecu_info.ecu_id = "1"
    eui.ecu_info.version = "0.5.1"
    eui.ecu_info.independent = True
    eui.url = (
        "http://127.0.0.1:8080"
    )
    eui.metadata = "metadata.jwt"
    return update_req
