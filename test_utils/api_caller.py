import path_loader

import argparse
import yaml
import json
from pathlib import Path
import status_call
import update_call

from app.otaclient_v2_pb2 import UpdateRequest, UpdateRequestEcu

import logutil
import logging

logger = logutil.get_logger(__name__, logging.DEBUG)


def load_ecu_info(ecu_info_file: str) -> dict:
    with open(ecu_info_file, "r") as f:
        return yaml.safe_load(f)


def load_external_update_request(request_yaml_file: str) -> UpdateRequest:
    with open(request_yaml_file, "r") as f:
        request_yaml: dict = yaml.safe_load(f)
        logger.debug(f"load external request: {request_yaml!r}")

        request = UpdateRequest(
            ecu=[
                UpdateRequestEcu(
                    ecu_id=request_yaml.get("ecu_id", "autoware"),
                    version=request_yaml.get("version", "123.x"),
                    url=request_yaml.get("url", "http://10.6.65.3:8080"),
                    cookies=json.dumps(
                        request_yaml.get("cookies", '{"test": "my-cookie"}')
                    ),
                )
            ],
        )

    return request


def main(args: argparse.Namespace):
    ecu_info = load_ecu_info(args.ecu_info)

    target = args.target
    # default to call the main ecu
    ecu_id = ecu_info.get("ecu_id")
    ecu_ipaddr = ecu_info.get("ip_addr")
    ecu_port = ecu_info.get("port", "50051")

    # search for the subecu by ecu_id
    if target != "main":
        found = False
        for subecu in ecu_info.get("secondaries", []):
            if subecu.get("ecu_id") == target:
                ecu_id = subecu.get("ecu_id")
                ecu_ipaddr = subecu.get("ip_addr")
                ecu_port = subecu.get("port", "50051")
                found = True
                break

        if not found:
            logger.warning(f"target ecu {target} is not found, use main ecu as target")

    logger.debug(f"{ecu_id=}, {ecu_ipaddr=}")
    cmd = args.command
    if cmd == "update":
        request = load_external_update_request(args.request)
        update_call.call_update(
            ecu_ip=ecu_ipaddr,
            ecu_port=ecu_port,
            request=request,
        )
    elif cmd == "status":
        status_call.call_status(
            ecu_ip=ecu_ipaddr,
            ecu_port=ecu_port,
            interval=args.interval,
        )


if __name__ == "__main__":
    logger.debug(f"load path by {path_loader.__name__}")
    parser = argparse.ArgumentParser(description="calling main ECU's API")
    parser.add_argument(
        "-c",
        "--ecu_info",
        type=str,
        default="ecu_info.yaml",
        help="ecu_info file to configure the caller(default: ecu_info.yaml)",
    )
    parser.add_argument("command", help="API to call, available API: update, status")
    parser.add_argument(
        "-t",
        "--target",
        default="main",
        help="indicate the API call's target(default: the main ecu)",
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=float,
        default=1,
        help="(status) pulling interval in second for status API call(default: 1)",
    )
    parser.add_argument(
        "-r",
        "--request",
        default="update_request.yaml",
        help="(update) yaml file that contains the request to send(default: update_request.yaml)",
    )

    args = parser.parse_args()
    if args.command not in {"update", "status"}:
        parser.error(f"unknown API: {args.command} (available: update, status)")
    if not Path(args.ecu_info).is_file():
        parser.error("input ecu_info file {args.ecu_info} not found!")

    main(args)
