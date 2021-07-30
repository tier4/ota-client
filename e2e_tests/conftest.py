import pytest
import pathlib
from pytest_xprocess import ProcessStarter

from ota_client import OtaClient
from ota_client_service import OtaClientService

from params_for_test import *

############ test configures & consts ############
# TODO: find a way to define the working_dir
@pytest.fixture(scope="session", autouse=True)
def dir_list(
    tmp_path_factory: pathlib.Path,
    working_dir: str,
):

    dir_list = {
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

    for _, path in dir_list.items():
        path.mkdir(parents=True, exist_ok=True)

    return dir_list


########### files needed for testing ###########
# create and set the contents to initial status

@pytest.fixture(scope="session")
def ota_status_file(dir_list):
    OTA_STATUS = "NORMAL"
    ota_status_file = dir_list["OTA_DIR"] / "ota_status"
    ota_status_file.write_text(OTA_STATUS)
    return ota_status_file


@pytest.fixture(scope="session")
def ecuid_file(dir_list):
    ecuid_file = dir_list["OTA_DIR"] / "ecuid"
    ecuid_file.write_text(ECUID)
    return ecuid_file


@pytest.fixture(scope="session")
def ecuinfo_yaml_file(dir_list):
    ecuinfo_yaml_file = dir_list["OTA_DIR"] / "ecuinfo.yaml"
    ecuinfo_yaml_file.write_text(ECUINFO_YAML)
    return ecuinfo_yaml_file


@pytest.fixture(scope="session")
def bankinfo_file(dir_list):
    bankinfo = dir_list["OTA_DIR"] / "bankinfo.yaml"
    bankinfo.write(BANK_INFO)
    return bankinfo


@pytest.fixture(scope="session")
def grub_file_default(dir_list):
    grub_file = dir_list["ETC_DIR"] / "default_grub"
    grub_file.write(GRUB_DEFAULT)
    return grub_file


@pytest.fixture(scope="session")
def custom_cfg_file(dir_list):
    custom_cfg = dir_list["GRUB_DIR"] / "custom.cfg"
    custom_cfg.write(GRUB_CUSTOM_CFG)
    return custom_cfg


@pytest.fixture(scope="session")
def fstab_file(dir_list):
    fstab_file = dir_list["ETC_DIR"] / "fstab"
    fstab_file.write(FSTAB_BY_UUID_BANKA)
    return fstab_file


@pytest.fixture(scope="session")
def grub_cfg_file(dir_list):
    grub = dir_list["GRUB_DIR"] / "grub.cfg"
    grub.write_text(grub_cfg_custom_cfg_params["grub_cfg"])


########### function fixture ###########

########### ota client instances #########

# TODO: mock OtaClient and GrubCtl
@pytest.fixture(scope="session")
def ota_client_instance(
    mocker,
    dir_list,
    ota_status_file,
    ecuid,
    ecuinfo_yaml,
    bankinfo_file,
    grub_file_default,
    grub_cfg_file,
    custom_cfg_file,
    fstab_file,
):
    import grub_control
    import ota_status

    # temporary assign a mock object during ota_client_instance init
    grubctl_mock = mocker.Mock(spec=grub_control.GrubCtl)
    otastatus_mock = mocker.Mock(spec=ota_status.OtaStatus)
    with mocker.patch("grub_control.GrubCtl", return_value=grubctl_mock), mocker.patch(
        "ota_status.OtaStatus", return_value=otastatus_mock
    ):

        ota_client_instance = OtaClient(
            boot_status="NORMAL_BOOT",
            ota_status_file=ota_status_file,
            bank_info_file=bankinfo_file,
            ecuid_file=ecuid,
            ecuinfo_yaml_file=ecuinfo_yaml,
        )

    # set the attribute of otaclient
    # TODO: init rollback dir and fstab file
    setattr(ota_client_instance, "_ota_dir", dir_list["OTA_DIR"])
    setattr(ota_client_instance, "_rollback_dir", dir_list["OTA_DIR"] / "rollback")
    setattr(ota_client_instance, "_grub_dir", dir_list["GRUB_DIR"])
    setattr(ota_client_instance, "_catalog_file", dir_list["OTA_DIR"] / ".catalog")
    setattr(ota_client_instance, "_mount_point", dir_list["MOUNT_POINT"])
    setattr(ota_client_instance, "_fstab_file", fstab_file)

    # set a real GrubCtl object and OtaStatus object to the instance
    # TODO: patch GrubCtl class:
    #   1. patch reboot
    #   2. patch any sys calls that may impact the OS
    mocker.patch.object(grub_control.os.sync)
    mocker.patch.object(grub_control.GrubCtl, "reboot", return_value=True)
    mocker.patch.object(grub_control.GrubCtl, "set_next_bank_boot", return_value=True)

    # TODO: ensure the logic of getting active bank and next bank when doing OTA update
    grub_ctl_object = grub_control.GrubCtl(
        default_grub_file=grub_file_default,
        grub_config_file=grub_cfg_file,
        custom_config_file=custom_cfg_file,
        bank_info_file=bankinfo_file,
        fstab_file=fstab_file,
    )
    ota_status_object = ota_status.OtaStatus(
        ota_status_file=ota_status_file,
        ota_rollback_file=dir_list["BOOT_DIR"] / "ota_rollback_count",
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
    eui.url = "http://127.0.0.1:8080"
    eui.metadata = "metadata.jwt"
    return update_req


########## OTA baseimage server ###########

# create background http server to serve ota baseimage
@pytest.fixture(scope="session", autouse=True)
def ota_server(xprocess):
    class ServerStarter(ProcessStarter):
        pattern = "Serving HTTP"
        max_read_lines = 3
        args = ["sudo", "-E", "python3", "-m", "http.server"]

    xprocess.ensure("ota_server", ServerStarter)
    yield
    xprocess.getinfo("ota_server").terminate()
