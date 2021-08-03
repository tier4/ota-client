#!/usr/bin/env python3

import argparse
import sys
import os

from ota_status import OtaStatus
from ota_boot import OtaBoot
from ota_client import OtaClient
from concurrent import futures

import grpc
import otaclient_pb2
import otaclient_pb2_grpc

from logging import getLogger, INFO, DEBUG

logger = getLogger(__name__)
logger.setLevel(INFO)


class OtaClientService(otaclient_pb2_grpc.OtaClientServiceServicer):
    """
    OTA lient service class
    """

    def __init__(self, otaclient):
        self._ota_client = otaclient

    def OtaUpdate(self, request, context):
        logger.info(f"request: {request}")
        # do update
        logger.info(context)
        result = self._ota_update(request)
        update_reply_msg = otaclient_pb2.OtaUpdateReply()
        if result:
            update_reply_msg.result = (
                otaclient_pb2.UpdateResultType.UPDATE_DOWNLOAD_SUCCESS
            )
        else:
            update_reply_msg.result = otaclient_pb2.UpdateResultType.UPDATE_FAIL
        logger.info(f"reply {update_reply_msg}")
        return update_reply_msg

    def OtaRollback(self, request, context):
        logger.info(f"request: {request}")
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
        logger.info(f"reply {rollback_reply_msg}")
        return rollback_reply_msg

    def OtaReboot(self, request, context):
        logger.info(f"request: {request}")
        # do reboot
        logger.info("OTA reboot request!")
        self._ota_reboot()
        reboot_reply_msg = otaclient_pb2.OtaRebootReply()
        logger.info(f"reply {reboot_reply_msg}")
        return reboot_reply_msg

    @staticmethod
    def _conv_ecu_status(ecu_status):
        # convert ecu_status to gRPC ecu_status
        ecu_status_table = {
            OtaStatus.NORMAL_STATE: otaclient_pb2.EcuStatusType.ECU_STATUS_NORMAL,
            OtaStatus.UPDATE_STATE: otaclient_pb2.EcuStatusType.ECU_STATUS_UPDATING,
            OtaStatus.PREPARED_STATE: otaclient_pb2.EcuStatusType.ECU_STATUS_DOWNLOADED,
            OtaStatus.SWITCHA_STATE: otaclient_pb2.EcuStatusType.ECU_STATUS_REBOOT,
            OtaStatus.SWITCHB_STATE: otaclient_pb2.EcuStatusType.ECU_STATUS_REBOOT,
            OtaStatus.UPDATE_FAIL_STATE: otaclient_pb2.EcuStatusType.ECU_STATUS_UPDATE_ERROR,
            OtaStatus.ROLLBACK_STATE: otaclient_pb2.EcuStatusType.ECU_STATUS_ROLLBACK,
            OtaStatus.ROLLBACKA_STATE: otaclient_pb2.EcuStatusType.ECU_STATUS_REBOOT,
            OtaStatus.ROLLBACKB_STATE: otaclient_pb2.EcuStatusType.ECU_STATUS_REBOOT,
            OtaStatus.ROLLBACK_FAIL_STATE: otaclient_pb2.EcuStatusType.ECU_STATUS_ROLLBACK_ERROR,
        }
        try:
            ecu_status_pb2 = ecu_status_table[ecu_status]
        except:
            ecu_status_pb2 = otaclient_pb2.EcuStatusType.ECU_STATUS_UNKNOWN
        return ecu_status_pb2

    @staticmethod
    def _conv_ecu_boot_status(boot_status):
        # convert ecu boot_status to gRPC boot_status
        boot_status_table = {
            OtaBoot.NORMAL_BOOT: otaclient_pb2.BootStatusType.NORMAL_BOOT,
            OtaBoot.SWITCH_BOOT: otaclient_pb2.BootStatusType.SWITCH_BOOT,
            OtaBoot.SWITCH_BOOT_FAIL: otaclient_pb2.BootStatusType.SWITCHING_BOOT_FAIL,
            OtaBoot.ROLLBACK_BOOT: otaclient_pb2.BootStatusType.ROLLBACK_BOOT,
            OtaBoot.ROLLBACK_BOOT_FAIL: otaclient_pb2.BootStatusType.ROLLBACK_BOOT_FAIL,
            OtaBoot.UPDATE_INCOMPLETE: otaclient_pb2.BootStatusType.UPDATE_INCOMPLETE,
            OtaBoot.ROLLBACK_INCOMPLETE: otaclient_pb2.BootStatusType.ROLLBACK_INCOMPLETE,
        }
        try:
            boot_status_pb2 = boot_status_table[boot_status]
        except:
            boot_status_pb2 = otaclient_pb2.BootStatusType.UNKNOWN
        return boot_status_pb2

    def EcuStatus(self, request, context):
        logger.info(f"request: {request}")
        # return ECU status info
        ecu_status = self._ota_client.get_ota_status()
        logger.info(f"ECU status: {ecu_status}")
        boot_status = self._ota_client.get_boot_status()
        logger.info(f"ECU boot status: {boot_status}")
        reply = otaclient_pb2.EcuStatusReply(
            status=self._conv_ecu_status(ecu_status),
            boot_status=self._conv_ecu_boot_status(boot_status),
        )
        logger.info(f"reply {reply}")
        return reply

    def EcuVersion(self, request, context):
        logger.info(f"request: {request}")
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
        logger.info(f"reply {ver_reply_msg}")
        return ver_reply_msg

    @staticmethod
    def _find_ecuinfo(ecuupdateinfo_list, ecu_id):
        """"""
        ecu_info = None
        for ecuupdateinfo in ecuupdateinfo_list:
            if ecu_id == ecuupdateinfo.ecu_info.ecu_id:
                logger.debug(f"[found] id={ecuupdateinfo.ecu_info.ecu_id}")
                ecu_info = ecuupdateinfo
        return ecu_info

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
        my_update_info = self._find_ecuinfo(
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
        my_rollback_info = self._find_ecuinfo(
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
    logging.basicConfig(
        format="%(asctime)s[%(levelname).3s][%(filename)s:%(funcName)s:(%(lineno)%d)] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

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
