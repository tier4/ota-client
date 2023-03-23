# Copyright 2022 TIER IV, INC. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import argparse
import asyncio
import yaml
import sys
from pathlib import Path

try:
    import otaclient  # noqa: F401
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from . import _logutil, _status_call, _update_call

logger = _logutil.get_logger(__name__)


async def main(args: argparse.Namespace):
    with open(args.ecu_info, "r") as f:
        ecu_info = yaml.safe_load(f)
        assert isinstance(ecu_info, dict)

    target_ecu_id = args.target
    # by default, send request to main ECU
    try:
        ecu_id = ecu_info["ecu_id"]
        ecu_ip = ecu_info["ip_addr"]
        ecu_port = 50051
    except KeyError:
        raise ValueError(f"invalid ecu_info: {ecu_info=}")

    if target_ecu_id != ecu_info.get("ecu_id"):
        found = False
        # search for target by ecu_id
        for subecu in ecu_info.get("secondaries", []):
            try:
                if subecu["ecu_id"] == target_ecu_id:
                    ecu_id = subecu["ecu_id"]
                    ecu_ip = subecu["ip_addr"]
                    ecu_port = int(subecu.get("port", 50051))
                    found = True
                break
            except KeyError:
                continue

        if not found:
            logger.critical(f"target ecu {target_ecu_id} is not found")
            sys.exit(-1)

    logger.info(f"send request to target ecu: {ecu_id=}, {ecu_ip=}")
    cmd = args.command
    if cmd == "update":
        await _update_call.call_update(
            ecu_id,
            ecu_ip,
            ecu_port,
            request_file=args.request,
        )
    elif cmd == "status":
        await _status_call.call_status(
            ecu_id,
            ecu_ip,
            ecu_port,
            interval=args.interval,
            count=args.count,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="calling ECU's API",
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
        help="indicate the target for the API request",
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=float,
        default=1,
        help="(status) polling interval in second for status API call",
    )
    parser.add_argument(
        "-C",
        "--count",
        type=int,
        default=0,
        help="(status) polling <count> time and then exits, value<=0 means inf",
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

    asyncio.run(main(args))
