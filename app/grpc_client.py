#!/usr/bin/env python3

#
# # OTA Client IF client
#

from __future__ import print_function
import logging

import argparse

import json
import base64
from OpenSSL import crypto


import grpc

import otaclient_pb2
import otaclient_pb2_grpc

from logging import getLogger, INFO, DEBUG

logger = getLogger(__name__)
logger.setLevel(INFO)


def get_policy_json_str(policy_json_file):
    """
    get policy json string
    """
    with open(policy_json_file, "r") as f:
        policy_json = json.dumps(json.load(f)).replace(" ", "")
        logger.debug(f"policy: {str(policy_json)}")
        return policy_json


def gen_policy(policy_json_file):
    """"""
    policy_str = get_policy_json_str(policy_json_file).encode()
    logger.debug(f"policy_str: {policy_str}")
    policy = (
        base64.b64encode(policy_str)
        .decode()
        .replace("+", "-")
        .replace("/", "~")
        .replace("=", "_")
    )
    logger.debug(f"policy: {policy}")
    return policy


def gen_sign(policy_json_file, pem_file):
    """"""
    with open(pem_file, "br") as f:
        private_pem = f.read()
        private_key = crypto.load_privatekey(crypto.FILETYPE_PEM, private_pem)
        payload = get_policy_json_str(policy_json_file).replace("\n", "").encode()
        signature = crypto.sign(private_key, payload, "sha1")
        sign = (
            base64.b64encode(signature)
            .decode()
            .replace("+", "-")
            .replace("/", "~")
            .replace("=", "_")
        )
        logger.debug(f"hash: {sign}")
        return sign


def get_key_id():
    """
    get KEY_ID (temp.)
    """
    return "K2JIMUVA7KL9FE"


def setup_cookie(policy_json_file, pem_file):
    """"""
    policy = gen_policy(policy_json_file)
    sign = gen_sign(policy_json_file, pem_file)
    key_id = get_key_id()
    params = (
        "CloudFront-Policy="
        + policy
        + ";CloudFront-Signature="
        + sign
        + ";CloudFront-Key-Pair-Id="
        + key_id
    )
    return "Cookie:" + params


def update(service_port):
    # NOTE(gRPC Python Team): .close() is possible on a channel and should be
    # used in circumstances in which the with statement does not fit the needs
    # of the code.
    with grpc.insecure_channel(service_port) as channel:
        # ecu_info = otaclient_pb2.EcuInfo()
        update_req = otaclient_pb2.OtaUpdateRequest()
        update_req.version = "0.0.1"
        eui = update_req.ecu_update_info.add()
        eui.ecu_info.ecu_name = "AutowareECU"
        eui.ecu_info.ecu_type = "autoware"
        eui.ecu_info.ecu_id = "1"
        eui.ecu_info.version = "0.5.1"
        eui.ecu_info.independent = True
        eui.url = (
            "https://d30joxnqr7qetz.cloudfront.net/test_proj/test_type/release/0.5.1"
        )
        eui.metadata = "metadata.jwt"
        eui.header = setup_cookie("tests/policy.json", "tests/private_key.pem")

        logger.debug(
            f"ecu_update_info: {update_req}",
        )
        stub = otaclient_pb2_grpc.OtaClientServiceStub(channel)
        response = stub.OtaUpdate(update_req)
        logger.debug(f"Ota Client IF client received: {response.result}")
        logger.debug(f"{response}")


def rollback(service_port):
    with grpc.insecure_channel(service_port) as channel:
        rollback_req = otaclient_pb2.OtaRollbackRequest()
        ei = rollback_req.ecu_info.add()
        ei.ecu_name = "AutowareMain"
        ei.ecu_type = "autoware"
        ei.ecu_id = "1"
        ei.version = "0.0.1"
        stub = otaclient_pb2_grpc.OtaClientServiceStub(channel)
        response = stub.OtaRollback(rollback_req)
        logger.debug(f"Ota Client Service client received: {response.result}")
        logger.debug(f"{response}")


def reboot(service_port):
    with grpc.insecure_channel(service_port) as channel:
        stub = otaclient_pb2_grpc.OtaClientServiceStub(channel)
        response = stub.OtaReboot(otaclient_pb2.OtaRebootRequest())
        logger.debug(f"{response}")


def ecustatus(service_port):
    with grpc.insecure_channel(service_port) as channel:
        stub = otaclient_pb2_grpc.OtaClientServiceStub(channel)
        response = stub.EcuStatus(otaclient_pb2.EcuStatusRequest())
        logger.debug(f"Ota Client Service client received: {response.status}")
        print(f"{response}")


def ecuversion(service_port):
    with grpc.insecure_channel(service_port) as channel:
        stub = otaclient_pb2_grpc.OtaClientServiceStub(channel)
        response = stub.EcuVersion(otaclient_pb2.EcuVersionRequest())
        logger.debug(f"Ota Client Service client received: {len(response.ecu_info)}")
        logger.debug(f"{response}")


if __name__ == "__main__":
    logging.basicConfig()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        help="command",
    )
    parser.add_argument(
        "--port", help="OTAClient server port", default="localhost:50051"
    )
    args = parser.parse_args()
    if args.command == "update":
        update(args.port)
    elif args.command == "rollback":
        rollback(args.port)
    elif args.command == "reboot":
        reboot(args.port)
    elif args.command == "status":
        ecustatus(args.port)
    elif args.command == "version":
        ecuversion(args.port)
    else:
        logger.error("parameter error!")
