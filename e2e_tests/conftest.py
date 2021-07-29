import pytest
import pathlib
from pytest_xprocess import ProcessStarter

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
ECUINFO_UPDATE_YAML = """\
main_ecu:
  ecu_name: 'autoware_ecu'
  ecu_type: 'autoware'
  ecu_id: '1'
  version: '0.5.1'
  independent: True
"""

# TODO: find a way to define the working_dir
@pytest.fixture(scope="session", autouse=True)
def configs(
    tmp_path_factory: pathlib.Path,
    working_dir: str,
    ):

    configs =  {
        "WORKDING_DIR": pathlib.Path(working_dir),
        "MOUNT_POINT": pathlib.Path(working_dir) / "mnt",
        "ETC_DIR": pathlib.Path(working_dir) / "etc",
        "GRUB_DIR": pathlib.Path(working_dir) / "boot/grub",
        "OTA_SOURCE_DIR": pathlib.Path(working_dir) / "data",
        "BOOT_DIR": tmp_path_factory / "boot",
        "OTA_DIR": tmp_path_factory / "boot/ota",
        "BANKA_DIR": pathlib.Path(working_dir) / "banka",
        "BANKB_DIR": pathlib.Path(working_dir) / "bankb",
    }

    return configs

# prepare files needed for the OTA
@pytest.fixture(scope="session", autouse=True)
def init_test_environment(configs):
    '''
    prepare ota_status file, bank_info_file, 
    ecuid_file and ecuinfo_yaml_file
    '''
    # make all needed folders here
    for _, path in configs.items():
        path.mkdir(parents=True, exist_ok=True)

    ota_d = configs["OTA_DIR"]
    ota_status_file = ota_d / "ota_status"
    bank_info_file = ota_d / "bankinfo.yaml"
    ecuid_file = ota_d / "ecuid"
    ecuinfo_yaml_file = ota_d / "ecuinfo.yaml"
    
    # prepare all needed initial files here
    # TODO: fstab, grub file, default grub file(configs["ETC_DIR"]/"default_grub")
    ota_status_file.write_text(OTA_STATUS)
    bank_info_file.write_text(BANK_INFO)
    ecuid_file.write_text(ECUID)
    ecuinfo_yaml_file.write_text(ECUINFO_YAML)

# TODO: check the scope of this patch, it should be last for the whole session
@pytest.fixture(scope="session", autouse=True)
def patch_ota_client_service(mocker):
    mocker.patch.object(OtaClientService, "_ota_reboot")

# generate a ota_client fixture for testing
# TODO: mock OtaClient init method/ mock GrubCtl
@pytest.fixture(scope="session")
def ota_client_instance(
    mocker, 
    configs,
    ):
    import grub_control
    import ota_status

    # temporary assign a mock object during ota_client_instance init
    grubctl_mock = mocker.Mock(spec=grub_control.GrubCtl)
    otastatus_mock = mocker.Mock(spec=ota_status.OtaStatus)
    with mocker.patch("grub_control.GrubCtl", return_value=grubctl_mock), \
        mocker.patch("ota_status.OtaStatus", return_value=otastatus_mock):

        ota_client_instance = OtaClient(
            boot_status="NORMAL_BOOT",
            ota_status_file=str(configs["OTA_DIR"] / "ota_status"),
            bank_info_file=str(configs["OTA_DIR"] / "bankinfo.yaml"),
            ecuid_file=str(configs["OTA_DIR"] / "ecuid"),
            ecuinfo_yaml_file=str(configs["OTA_DIR"] / "ecuinfo.yaml"),
        )

    # set the attribute of otaclient
    setattr(ota_client_instance, "_ota_dir", configs["OTA_DIR"])
    setattr(ota_client_instance, "_rollback_dir", configs["OTA_DIR"] / "rollback")
    setattr(ota_client_instance, "_grub_dir", configs["GRUB_DIR"])
    setattr(ota_client_instance, "_catalog_file", configs["OTA_DIR"] / ".catalog")
    setattr(ota_client_instance, "_mount_point", configs["MOUNT_POINT"])
    setattr(ota_client_instance, "_fstab_file", configs["ETC_DIR"] / "fstab")
    
    # set a real GrubCtl object and OtaStatus object to the instance
    grub_ctl_object = grub_control.GrubCtl(
        default_grub_file=configs["ETC_DIR"]/"default_grub",
        grub_config_file=configs["GRUB_DIR"]/"grub.cfg",
        custom_config_file=configs["GRUB_DIR"]/"custom.cfg",
        bank_info_file=configs["OTA_DIR"]/"bankinfo.yaml",
        fstab_file=configs["ETC_DIR"]/"fstab",
    )
    ota_status_object = ota_status.OtaStatus(
        ota_status_file=configs["BOOT_DIR"]/"ota_status",
        ota_rollback_file=configs["BOOT_DIR"]/"ota_rollback_count",
    )
    setattr(ota_client_instance, "_grub_ctl", grub_ctl_object)
    setattr(ota_client_instance, "_ota_status", ota_status_object)

    return ota_client_instance


# generate a ota_client_service fixture for testing
@pytest.fixture(scope="session", autouse=True)
def ota_client_service_instance(ota_client_instance):
    return OtaClientService(ota_client_instance)

# generate ota request
@pytest.fixture(scope="session")
def ota_request():
    import otaclient_pb2

    update_req = otaclient_pb2.OtaUpdateRequest()
    update_req.version = "0.0.1"
    eui = update_req.ecu_update_info.add()
    eui.ecu_info.ecu_name = "autoware_ecu"
    eui.ecu_info.ecu_type = "autoware"
    eui.ecu_info.ecu_id = "1"
    eui.ecu_info.version = "0.5.1"
    eui.ecu_info.independent = True
    eui.url = (
        "http://127.0.0.1:8080"
    )
    eui.metadata = "metadata.jwt"
    return update_req

# create background http server to serve ota baseimage
@pytest.fixture(scope="session", autouse=True)
def ota_server(xprocess):
    class ServerStarter(ProcessStarter):
        pattern = "Serving HTTP"
        max_read_lines = 3
        args = ['sudo', '-E', 'python3', '-m', 'http.server']

    xprocess.ensure("ota_server", ServerStarter)
    yield
    xprocess.getinfo("ota_server").terminate()