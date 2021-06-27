#!/usr/bin/env python3

import argparse
import sys
import os

from ota_boot import OtaBoot
from ota_client import OtaClient
from concurrent import futures

import grpc
import otaclient_pb2
import otaclient_pb2_grpc

from logging import getLogger, INFO, DEBUG

logger = getLogger(__name__)
logger.setLevel(INFO)


def _setup_ecus():
    """
    read ECU configuration
    """
    ecuinfo = {}
    ecuinfo["ecu_name"] = "Autoware"
    ecuinfo["ecu_type"] = "autoware"
    ecuinfo["ecu_id"] = "1"
    ecuinfo["version"] = "0.0.0"
    ecuinfo["independent"] = True
    return ecuinfo


class OtaClientService(otaclient_pb2_grpc.OtaClientServiceServicer):
    """
    OTA lient service class
    """
    def __init__(self, otaclient):
        self._ecuinfo = _setup_ecus()
        self._subecu_port = {}
        self._ota_client = otaclient

    def OtaUpdate(self, request, context):
        # do update
        result = self._ota_update(request)
        update_reply_msg = otaclient_pb2.OtaUpdateReply()
        if result:
            update_reply_msg.result = (
                otaclient_pb2.UpdateResultType.UPDATE_DOWNLOAD_SUCCESS
            )
        else:
            update_reply_msg.result = otaclient_pb2.UpdateResultType.UPDATE_FAIL
        return update_reply_msg

    def OtaRollback(self, request, context):
        # do rollback
        result = self._ota_rollback(request)
        rollback_reply_msg = otaclient_pb2.OtaRollbackReply()
        rollback_reply_msg.result = otaclient_pb2.RollbackResultType.ROLLBACK_SUCCESS
        ei = rollback_reply_msg.ecu_info.add()
        info = request.ecu_info[0]
        ei.ecu_name = info.ecu_name
        ei.ecu_type = info.ecu_type
        ei.ecu_id = info.ecu_id
        ei.version = info.version
        return rollback_reply_msg

    def OtaReboot(self, request, context):
        # do reboot
        logger.info("OTA reboot request!")
        self._ota_reboot()
        reboot_reply_msg = otaclient_pb2.OtaRebootReply()
        return reboot_reply_msg

    def EcuStatus(self, request, context):
        # return ECU status info
        ecu_status = self._ota_client.get_ota_status()
        logger.info(f"ECU status: {ecu_status}")
        status = status = otaclient_pb2.EcuStatusType.ECU_STATUS_NORMAL
        if ecu_status == "NORMAL":
            status = otaclient_pb2.EcuStatusType.ECU_STATUS_NORMAL
        elif ecu_status == "UPDATING":
            status = otaclient_pb2.EcuStatusType.ECU_STATUS_UPDATING
        elif ecu_status == "PREPARED":
            status = otaclient_pb2.EcuStatusType.ECU_STATUS_DOWNLOADED
        elif ecu_status == "SWITCHA" or ecu_status == "SWITCHB":
            status = otaclient_pb2.EcuStatusType.ECU_STATUS_SWITCH
        elif ecu_status == "ROLLBACKA" or ecu_status == "ROLLBACKB":
            status = otaclient_pb2.EcuStatusType.ECU_STATUS_ROLLBACK
        else:
            status = otaclient_pb2.EcuStatusType.ECU_STATUS_UNKNOWN
        boot_status = self._ota_client.get_boot_status()
        bstatus = otaclient_pb2.BootStatusType.NORMAL_BOOT
        if boot_status == "NORMAL_BOOT":
            bstatus = otaclient_pb2.BootStatusType.NORMAL_BOOT
        elif boot_status == "SWITCH_BOOT":
            bstatus = otaclient_pb2.BootStatusType.SWITCH_BOOT
        elif boot_status == "ROLLBACK_BOOT":
            bstatus = otaclient_pb2.BootStatusType.ROLLBACK_BOOT
        elif boot_status == "SWITCH_BOOT_FAIL":
            bstatus = otaclient_pb2.BootStatusType.SWITCH_BOOT_FAIL
        elif boot_status == "ROLLBACK_BOOT_FAIL":
            bstatus = otaclient_pb2.BootStatusType.ROLLBACK_BOOT_FAIL
        elif boot_status == "UPDATE_IMCOMPLETE":
            bstatus = otaclient_pb2.BootStatusType.UPDATE_IMCOMPLETE
        else:
            bstatus = otaclient_pb2.BootStatusType.UNKOWN
        return otaclient_pb2.EcuStatusReply(status=status, boot_status=bstatus)

    def EcuVersion(self, request, context):
        # Return ECU version info
        ver_reply_msg = otaclient_pb2.EcuVersionReply()
        ei = ver_reply_msg.ecu_info.add()
        ecu_info = self._ota_client.get_ecuinfo()
        ecuinf = ecu_info["main_ecu"]
        logger.debug(f"{ecuinf}")
        ei.ecu_name = ecuinf["ecu_name"]
        ei.ecu_type = ecuinf["ecu_type"]
        ei.ecu_id = ecuinf["ecu_id"]
        ei.version = ecuinf["version"]
        ei.independent = ecuinf["independent"]
        if "sub_ecus" in ecu_info:
            for ecuinf in ecu_info["sub_ecus"]:
                ei = ver_reply_msg.ecu_info.add()
                ei.ecu_name = ecuinf["ecu_name"]
                ei.ecu_type = ecuinf["ecu_type"]
                ei.ecu_id = ecuinf["ecu_id"]
                ei.version = ecuinf["version"]
                ei.independent = ecuinf["independent"]
        # print(ver_reply_msg)
        return ver_reply_msg

    def _ota_update(self, request):
        """
        OTA update function
        """
        result = True
        update_count = 0
        ecu_info = self._ota_client.get_ecuinfo()
        logger.debug(f"ecuinfo:{ecu_info}")
        if "sub_ecus" in ecu_info:
            # update sub-ECUs
            update_subs = []
            logger.info("Update sub ECUs.")
            for subecuinfo in ecu_info["sub_ecus"]:
                if self._subecu_update(subecuinfo, request):
                    update_count += 1
        # find my ECU info
        ecuupdateinfo = request.ecu_update_info
        logger.info(f"my ECU ID: {self._ota_client.get_my_ecuid()}")
        my_update_info = self._ota_client.find_ecuinfo(
            ecuupdateinfo, self._ota_client.get_my_ecuid()
        )
        if my_update_info is not None:
            logger.info("execute update!!")
            logger.debug(f"{my_update_info}")
            if self._ota_client.set_update_ecuinfo(my_update_info):
                result = self._ota_client.update(my_update_info)
                if result:
                    update_count += 1
        logger.debug(f"update_count: {update_count}")
        if update_count > 0:
            self._ota_client.save_update_ecuinfo()
            if self._ota_client.is_main_ecu():
                self._ota_reboot()

        return result

    def _subecu_update(self, sub_ecu_info, ecuinfo_list):
        """
        update sub-ECU
        """
        return True

    def _ota_reboot(self):
        """
        OTA reboot
        """
        ecu_info = self._ota_client.get_ecuinfo()
        if "sub_ecus" in ecu_info:
            # reboot sub-ECUs
            update_subs = []
            logger.info("reboot sub ECUs.")
            for subecuinfo in ecu_info["sub_ecus"]:
                self._subecu_reboot()
        # self rebbot
        self._ota_client.reboot()

    def _subecu_reboot(self):
        """"""
        return True

    def _ota_rollback(self, request):
        """
        OTA Rollback function
        """
        ecu_info = self._ota_client.get_ecuinfo()
        if "sub_ecus" in ecu_info:
            # update sub-ECUs
            update_subs = []
            logger.info("Rollback sub ECUs.")
            for subecuinfo in ecu_info["sub_ecus"]:
                self._subecu_rollback(subecuinfo, request)
        # find my ECU info
        ecurollbackinfo = request.ecu_rollback_info
        logger.debug(f"{ecurollbackinfo[0].ecu_info}")
        logger.debug(f"my ECU ID: {self._ota_client.get_my_ecuid}")
        my_rollback_info = self._ota_client.find_ecuinfo(
            ecurollbackinfo, self._ota_client.get_my_ecuid()
        )
        print(my_rollback_info)
        if my_rollback_info != {}:
            logger.info("execute update!")
            result = self._ota_client.update(my_rollback_info)
        else:
            result = True
        return result

    def _subecu_rollback(self, request):
        """"""
        return True


def _otaclient_service(otaclient, port):
    """
    OTA Client gRPC server service start
    """
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    otaclient_pb2_grpc.add_OtaClientServiceServicer_to_server(
        OtaClientService(otaclient), server
    )
    server.add_insecure_port(port)
    server.start()
    logger.info("OTA Client Service start!")
    server.wait_for_termination()


def _daemonize(port, no_boot=False):
    pid = os.fork()
    if pid > 0:
        # parent process
        pid_file = open("/var/run/ota_client.pid", "w")
        pid_file.write(str(pid) + "\n")
        pid_file.close()
        sys.exit()
    if pid == 0:
        # child process
        boot_result = "NORMAL_BOOT"
        if not args.no_boot:
            # otaboot = OtaBoot(ota_status_file='tests/ota_status', bank_info_file='tests/bankinfo.yaml')
            otaboot = OtaBoot()
            boot_result = otaboot.boot()
        # otaclient = OtaClient(boot_status=boot_result, ota_status_file='tests/ota_status', bank_info_file='tests/bankinfo.yaml', ecuid_file='tests/ecuid', ecuinfo_yaml_file='tests/ecuinfo.yaml')
        otaclient = OtaClient(boot_status=boot_result)
        _otaclient_service(otaclient, port)


if __name__ == "__main__":
    """
    OTA client service main
    """
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--daemonize", help="daemonize OTA Client service", default=False
    )
    parser.add_argument(
        "--port",
        help="OTAClient server port",
        default="localhost:50051",  #'[::]:50051' #
    )
    parser.add_argument("--no_boot", help="OTAClient no boot processing", default=False)
    args = parser.parse_args()

    if args.daemonize:
        logger.info("Daemonize!")
        _daemonize(args.port)
    else:
        boot_result = "NORMAL_BOOT"
        if not args.no_boot:
            # otaboot = OtaBoot(ota_status_file='tests/ota_status', bank_info_file='tests/bankinfo.yaml')
            otaboot = OtaBoot()
            boot_result = otaboot.boot()
        # otaclient = OtaClient(boot_status=boot_result, ota_status_file='tests/ota_status', bank_info_file='tests/bankinfo.yaml', ecuid_file='tests/ecuid', ecuinfo_yaml_file='tests/ecuinfo.yaml')
        otaclient = OtaClient(boot_status=boot_result)
        _otaclient_service(otaclient, port=args.port)
