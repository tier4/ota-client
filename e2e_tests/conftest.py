import os
import pytest
import pathlib
from pytest_xprocess import ProcessStarter

from ota_client import OtaClient
from ota_client_service import OtaClientService

from params_for_test import *

############ test configures & consts ############
@pytest.fixture(scope="session", autouse=True)
def configs_for_test():
    cfg = dict()

    # config the working dir
    working_dir = pathlib.Path(os.environ["WORKING_DIR"])
    if not working_dir.is_dir():
        print("Please specifc working dir via WORKING_DIR environment variable.")
        raise ValueError("Invalid working dir.")
    cfg["WORKING_DIR"] = working_dir

    # config the ota_server listen port
    try:
        port = int(os.environ["OTA_SERVER_PORT"])
        if port < 0 or port > 65535:
            print("Please specifc working dir via WORKING_DIR environment variable.")
            raise ValueError()
        cfg["OTA_SERVER_PORT"] = port
    except:
        print(f"Invalid port number, use default port {DEFAULT_OTA_SERVER_PORT}")
        cfg["OTA_SERVER_PORT"] = DEFAULT_OTA_SERVER_PORT

    return cfg


@pytest.fixture(scope="session", autouse=True)
def dir_list(configs_for_test):
    """
    list of dirs to used for testing
    dirs are all persisted for debug purpose
    """
    working_dir = configs_for_test["WORKING_DIR"]

    dir_list = {
        "WORKDING_DIR": working_dir,
        "MOUNT_POINT": working_dir / "mnt",
        "ETC_DIR": working_dir / "etc",
        "OTA_SOURCE_DIR": working_dir / "data",
        "BOOT_DIR": working_dir / "boot",
        "GRUB_DIR": working_dir / "boot/grub",
        "OTA_DIR": working_dir / "boot/ota",
        "ROLLBACK_DIR": working_dir / "boot/ota/rollback",
        "BANKA_DIR": working_dir / "banka",
        "BANKB_DIR": working_dir / "bankb",
    }

    for _, path in dir_list.items():
        path.mkdir(parents=True, exist_ok=True)

    return dir_list


########### files needed for testing ###########
# create and set the contents to initial status


@pytest.fixture(scope="session")
def ota_status_file(dir_list):
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
    grub.write_text(grub_cfg_wo_submenu)


########### function fixture ###########


########### ota client instances #########


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
    with mocker.patch.object(
        grub_control, "GrubCtl", return_value=mocker.Mock(spec=grub_control.GrubCtl)
    ), mocker.patch.object(
        ota_status, "OtaStatus", return_value=mocker.Mock(spec=ota_status.OtaStatus)
    ):
        ota_client_instance = OtaClient(
            boot_status=BOOT_STATUS,
            ota_status_file=ota_status_file,
            bank_info_file=bankinfo_file,
            ecuid_file=ecuid,
            ecuinfo_yaml_file=ecuinfo_yaml,
        )

    # set the attribute of otaclient
    setattr(ota_client_instance, "_ota_dir", dir_list["OTA_DIR"])
    setattr(ota_client_instance, "_rollback_dir", dir_list["ROLLBACK_DIR"])
    setattr(ota_client_instance, "_grub_dir", dir_list["GRUB_DIR"])
    setattr(ota_client_instance, "_catalog_file", dir_list["OTA_DIR"] / ".catalog")
    setattr(ota_client_instance, "_mount_point", dir_list["MOUNT_POINT"])
    setattr(ota_client_instance, "_fstab_file", fstab_file)

    # set a real GrubCtl object and OtaStatus object to the instance
    #   1. patch reboot
    #   2. patch any sys calls that may impact the OS
    mocker.patch.object(grub_control.os, "sync")
    mocker.patch.object(grub_control.GrubCtl, "reboot", return_value=True)
    mocker.patch.object(grub_control.GrubCtl, "set_next_bank_boot", return_value=True)

    mocker.patch.object(OtaClient, "_unmount_bank")
    mocker.patch.object(OtaClient, "_mount_bank")

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

    # assign patched grub_ctl and ota_status object to ota_client
    setattr(ota_client_instance, "_grub_ctl", grub_ctl_object)
    setattr(ota_client_instance, "_ota_status", ota_status_object)

    return ota_client_instance


# generate a ota_client_service fixture for testing
@pytest.fixture(scope="session", autouse=True)
def ota_client_service_instance(ota_client_instance):
    return OtaClientService(ota_client_instance)


# generate ota request
@pytest.fixture(scope="session")
def ota_request(configs_for_test):
    import otaclient_pb2

    update_req = otaclient_pb2.OtaUpdateRequest()
    update_req.version = "0.0.1"
    eui = update_req.ecu_update_info.add()
    eui.ecu_info.ecu_name = "autoware_ecu"
    eui.ecu_info.ecu_type = "autoware"
    eui.ecu_info.ecu_id = "1"
    eui.ecu_info.version = "0.5.1"
    eui.ecu_info.independent = True
    eui.url = "http://{}:{}".format("localhost", configs_for_test["OTA_SERVER_PORT"])
    eui.metadata = "metadata.jwt"
    return update_req


########## OTA baseimage server ###########

# create background http server to serve ota baseimage
@pytest.fixture(scope="session", autouse=True)
def ota_server(xprocess, configs_for_test):
    class ServerStarter(ProcessStarter):
        pattern = "Serving HTTP"
        max_read_lines = 3
        args = [
            "sudo",
            "-E",
            "python3",
            "-m",
            "http.server",
            "--directory",
            str(configs_for_test["WORKING_DIR"]),
            "--port",
            configs_for_test["OTA_SERVER_PORT"],
        ]

    xprocess.ensure("ota_server", ServerStarter)
    yield
    xprocess.getinfo("ota_server").terminate()
