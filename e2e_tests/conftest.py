import os
import sys
import pytest
import pathlib
from xprocess import ProcessStarter

from e2e_tests.params_for_test import *

############ path fix ############
@pytest.fixture(scope="session", autouse=True)
def pythonpath():
    sys.path.append(
        os.path.abspath(os.path.dirname(os.path.abspath(__file__)) + "/../app/")
    )


############ test configures & consts ############
@pytest.fixture(scope="module", autouse=True)
def configs_for_test():
    cfg = dict()

    # config the working dir
    try:
        working_dir = pathlib.Path(os.environ["WORKING_DIR"])
        if not working_dir.is_dir():
            raise ValueError("Invalid WORKING_DIR.")
    except:
        working_dir = pathlib.Path("/")
        print(f"WORKING_DIR: Using default value {working_dir}.")
    cfg["WORKING_DIR"] = working_dir

    # config the ota baseimage dir
    try:
        ota_image_dir = pathlib.Path(os.environ["OTA_IMAGE_DIR"])
        if not ota_image_dir.is_dir():
            raise ValueError("Invalid OTA_IMAGE_DIR.")
    except:    
        ota_image_dir = pathlib.Path("/ota-image")
        print(f"OTA_IMAGE_DIR: Using default value {ota_image_dir}.")
    cfg["OTA_IMAGE_DIR"] = ota_image_dir

    # config the ota_server listen port
    try:
        port = int(os.environ["OTA_SERVER_PORT"])
        if port < 0 or port > 65535:
            raise ValueError()
        cfg["OTA_SERVER_PORT"] = port
    except:
        print(f"Use default port {DEFAULT_OTA_SERVER_PORT}")
        cfg["OTA_SERVER_PORT"] = DEFAULT_OTA_SERVER_PORT

    return cfg


@pytest.fixture(scope="module", autouse=True)
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


@pytest.fixture(scope="module")
def ota_status_file(dir_list):
    ota_status_file = dir_list["OTA_DIR"] / "ota_status"
    ota_status_file.write_text(OTA_STATUS)
    return ota_status_file


@pytest.fixture(scope="module")
def ecuid_file(dir_list):
    ecuid_file = dir_list["OTA_DIR"] / "ecuid"
    ecuid_file.write_text(ECUID)
    return ecuid_file


@pytest.fixture(scope="module")
def ecuinfo_yaml_file(dir_list):
    ecuinfo_yaml_file = dir_list["OTA_DIR"] / "ecuinfo.yaml"
    ecuinfo_yaml_file.write_text(ECUINFO_YAML)
    return ecuinfo_yaml_file


@pytest.fixture(scope="module")
def bankinfo_file(dir_list):
    bankinfo = dir_list["OTA_DIR"] / "bankinfo.yaml"
    bankinfo.write_text(BANK_INFO)
    return bankinfo


@pytest.fixture(scope="module")
def grub_file_default(dir_list):
    grub_file = dir_list["ETC_DIR"] / "default/grub"
    # grub_file.write_text(GRUB_DEFAULT)
    return grub_file


@pytest.fixture(scope="module")
def custom_cfg_file(dir_list):
    custom_cfg = dir_list["GRUB_DIR"] / "custom.cfg"
    custom_cfg.write_text(GRUB_CUSTOM_CFG)
    return custom_cfg


@pytest.fixture(scope="module")
def fstab_file(dir_list):
    fstab_file = dir_list["ETC_DIR"] / "fstab"
    fstab_file.write_text(FSTAB_BY_UUID_BANKA)
    return fstab_file


@pytest.fixture(scope="module")
def grub_cfg_file(dir_list):
    grub = dir_list["GRUB_DIR"] / "grub.cfg"
    grub.write_text(grub_cfg_wo_submenu)


########### function fixture ###########


########### ota client instances #########

@pytest.fixture(scope="module")
def ota_client_instance(
    module_mocker,
    dir_list,
    ota_status_file,
    ecuid_file,
    ecuinfo_yaml_file,
    bankinfo_file,
    grub_file_default,
    grub_cfg_file,
    custom_cfg_file,
    fstab_file,
    pythonpath,
):
    import grub_control
    import ota_status
    import ota_client

    # patch the ota_client
    module_mocker.patch.object(ota_client, "_unmount_bank")
    module_mocker.patch.object(ota_client, "_mount_bank")
    module_mocker.patch.object(ota_client.OtaClient, "_get_next_bank", return_value=str(dir_list["BANKB_DIR"]))
    module_mocker.patch.object(ota_client.OtaClient, "_get_current_bank", return_value=str(dir_list["BANKA_DIR"]))
    module_mocker.patch.object(ota_client.OtaClient, "_get_current_bank_uuid", return_value=BANKA_UUID)
    module_mocker.patch.object(ota_client.OtaClient, "_get_next_bank_uuid", return_value=BANKB_UUID)
    # set a real GrubCtl object and OtaStatus object to the instance
    #   1. patch reboot
    #   2. patch any sys calls that may impact the OS
    module_mocker.patch.object(grub_control.os, "sync")
    module_mocker.patch.object(grub_control.GrubCtl, "reboot", return_value=True)
    module_mocker.patch.object(grub_control.GrubCtl, "set_next_bank_boot", return_value=True)
    # mock out the whole BankInfo object 
    module_mocker.patch.object(grub_control, "BankInfo")

    # temporary assign a mock object during ota_client_instance init
    with module_mocker.patch.object(
        ota_client, "GrubCtl", return_value=module_mocker.Mock(spec=grub_control.GrubCtl)
    ), module_mocker.patch.object(
        ota_client, "OtaStatus", return_value=module_mocker.Mock(spec=ota_status.OtaStatus)
    ):
        ota_client_instance = ota_client.OtaClient(
            boot_status=BOOT_STATUS,
            ota_status_file=str(ota_status_file),
            bank_info_file=str(bankinfo_file),
            ecuid_file=str(ecuid_file),
            ecuinfo_yaml_file=str(ecuinfo_yaml_file),
        )

    # set the attribute of otaclient
    setattr(ota_client_instance, "_ota_dir", dir_list["OTA_DIR"])
    setattr(ota_client_instance, "_rollback_dir", dir_list["ROLLBACK_DIR"])
    setattr(ota_client_instance, "_grub_dir", dir_list["GRUB_DIR"])
    setattr(ota_client_instance, "_catalog_file", dir_list["OTA_DIR"] / ".catalog")
    setattr(ota_client_instance, "_mount_point", dir_list["MOUNT_POINT"])
    setattr(ota_client_instance, "_fstab_file", fstab_file)

    grub_ctl_object = grub_control.GrubCtl(
        default_grub_file=str(grub_file_default),
        grub_config_file=str(grub_cfg_file),
        custom_config_file=str(custom_cfg_file),
        bank_info_file=str(bankinfo_file),
        fstab_file=str(fstab_file),
    )

    ota_status_object = ota_status.OtaStatus(
        ota_status_file=str(ota_status_file),
        ota_rollback_file=str(dir_list["BOOT_DIR"] / "ota_rollback_count"),
    )

    # assign patched grub_ctl and ota_status object to ota_client
    setattr(ota_client_instance, "_grub_ctl", grub_ctl_object)
    setattr(ota_client_instance, "_ota_status", ota_status_object)

    return ota_client_instance


# generate a ota_client_service fixture for testing
@pytest.fixture(scope="module")
def ota_client_service_instance(ota_client_instance):
    from ota_client_service import OtaClientService

    return OtaClientService(ota_client_instance)


# generate ota request
@pytest.fixture(scope="module")
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
# this server will last for the whole session
@pytest.fixture(scope="module")
def ota_server(xprocess, configs_for_test):
    class ServerStarter(ProcessStarter):
        timeout = 3
        pattern = "Serving HTTP"
        max_read_lines = 3
        terminate_on_interrupt = True
        args = [
            "python3",
            "-u",
            "-m",
            "http.server",
            "--directory",
            str(configs_for_test["OTA_IMAGE_DIR"]),
            configs_for_test["OTA_SERVER_PORT"],
        ]

    xprocess.ensure("ota_server", ServerStarter)
    yield
    xprocess.getinfo("ota_server").terminate()
