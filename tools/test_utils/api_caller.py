import argparse
import yaml
import sys
from pathlib import Path

try:
    import otaclient  # noqa: F401
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))


from . import _logutil, _status_call, _update_call

logger = _logutil.get_logger(__name__)


def main(args: argparse.Namespace):
    with open(args.ecu_info, "r") as f:
        ecu_info = yaml.safe_load(f)

    target_ecu_id = args.target
    # load main ecu info
    ecu_id = ecu_info.get("ecu_id")
    ecu_ip = ecu_info.get("ip_addr")
    ecu_port = 50051

    if target_ecu_id != ecu_info.get("ecu_id"):
        found = False
        # search for target by ecu_id
        for subecu in ecu_info.get("secondaries", []):
            if subecu.get("ecu_id") == target_ecu_id:
                ecu_id = subecu.get("ecu_id")
                ecu_ip = subecu.get("ip_addr")
                ecu_port = int(subecu.get("port", 50051))
                found = True
                break
        if not found:
            logger.critical(f"target ecu {target_ecu_id} is not found")
            sys.exit(-1)

    logger.debug(f"target ecu: {ecu_id=}, {ecu_ip=}")
    cmd = args.command
    if cmd == "update":
        _update_call.call_update(
            ecu_id,
            ecu_ip,
            ecu_port,
            request_file=args.request,
        )
    elif cmd == "status":
        _status_call.call_status(
            ecu_id,
            ecu_ip,
            ecu_port,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="calling main ECU's API",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-c",
        "--ecu_info",
        type=str,
        default="test_utils/ecu_info.yaml",
        help="ecu_info file to configure the caller",
    )
    parser.add_argument("command", help="API to call, available API: update, status")
    parser.add_argument(
        "-t",
        "--target",
        default="autoware",
        help="indicate the API call's target",
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=float,
        default=1,
        help="(status) pulling interval in second for status API call",
    )
    parser.add_argument(
        "-r",
        "--request",
        default="test_utils/update_request.yaml",
        help="(update) yaml file that contains the request to send",
    )

    args = parser.parse_args()
    if args.command not in {"update", "status"}:
        parser.error(f"unknown API: {args.command} (available: update, status)")
    if not Path(args.ecu_info).is_file():
        parser.error(f"ecu_info file {args.ecu_info} not found!")
    if args.command == "update" and not Path(args.request).is_file():
        parser.error(f"update request file {args.request} not found!")

    main(args)
