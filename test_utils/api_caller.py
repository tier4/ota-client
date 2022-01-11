###### load path ######
def _path_load():
    import sys
    from pathlib import Path

    project_base = Path(__file__).absolute().parent.parent
    sys.path.extend([str(project_base), str(project_base/"app")])

_path_load()
######

import argparse
import yaml
from pathlib import Path
from status_call import call_status
import update_call

import logutil
import logging

logger = logutil.get_logger(__name__, logging.DEBUG)

def load_ecu_info(ecu_info_file: str) -> dict:
    with open(ecu_info_file, "r") as f:
        return yaml.safe_load(f)

def main(args: argparse.Namespace):
    from app.ota_client_call import OtaClientCall

    caller = OtaClientCall()
    ecu_info = load_ecu_info(args.ecu_info)

    target = args.target
    # default to call the main ecu
    ecu_id = ecu_info.get("ecu_id", None)
    ecu_ipaddr = ecu_info.get("ip_addr", None)
    ecu_port = ecu_info.get("port", "50051")

    # search for the subecu by ecu_id
    if target != "main":
        found = False
        for subecu in ecu_info.get("secondaries", []):
            if subecu.get("ecu_id", None) == target:
                ecu_id = subecu.get("ecu_id", None)
                ecu_ipaddr = subecu.get("ip_addr", None)
                ecu_port = subecu.get("port", "50051")
                found = True
                break

        if not found:
            logger.warning(f"target ecu {target} is not found, use main ecu as target")
    
    logger.debug(f"{ecu_id=}, {ecu_ipaddr=}")
    cmd = args.command
    if cmd == "update":
        request = update_call.load_external_update_request(args.request)
        update_call.call_update(     
            caller=caller,
            ecu_ip=ecu_ipaddr,
            ecu_port=ecu_port,
            request=request,
            )
    elif cmd == "status":
        call_status(
            caller=caller,
            ecu_ip=ecu_ipaddr,
            ecu_port=ecu_port,
            interval=args.interval,
        )

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="calling main ECU's API")
    parser.add_argument(
        "-c", "--ecu_info",
        type=str, default="ecu_info.yaml",
        help="ecu_info file to configure the caller(default: ecu_info.yaml)",
    )
    parser.add_argument("command", help="API to call, available API: update, status")
    parser.add_argument(
        "-t", "--target", default="main",
        help="indicate the API call's target(default: the main ecu)"
        )
    parser.add_argument(
        "-i", "--interval", type=float, default=1,
        help="(status) pulling interval in second for status API call(default: 1)"
        )
    parser.add_argument(
        "-r", "--request", default="update_request.yaml",
        help="(update) yaml file that contains the request to send(default: update_request.yaml)"
    )

    args = parser.parse_args()
    if args.command not in {"update", "status"}:
        parser.error(f"unknown API: {args.command} (available: update, status)")
    if not Path(args.ecu_info).is_file():
        parser.error("input ecu_info file {args.ecu_info} not found!")

    main(args)