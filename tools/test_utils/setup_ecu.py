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
import sys
from pathlib import Path
from typing import List

import grpc
import yaml

try:
    import otaclient  # noqa: F401
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from otaclient.app.ota_client_service import service_wait_for_termination
from otaclient.app.proto import v2, v2_grpc

from . import _logutil

logger = _logutil.get_logger(__name__)

_DEFAULT_PORT = 50051
_MODE = {"standalone", "mainecu", "subecus"}


class MiniOtaClientServiceV2(v2_grpc.OtaClientServiceServicer):
    UPDATE_TIME_COST = 10
    REBOOT_INTERVAL = 5

    def __init__(self, ecu_id: str):
        self.ecu_id = ecu_id
        self._lock = asyncio.Lock()
        self._in_update = asyncio.Event()
        self._rebooting = asyncio.Event()

    async def _on_update(self):
        await asyncio.sleep(self.UPDATE_TIME_COST)
        logger.debug(f"{self.ecu_id=} finished update, rebooting...")
        self._rebooting.set()
        await asyncio.sleep(self.REBOOT_INTERVAL)
        self._rebooting.clear()
        self._in_update.clear()

    async def Update(self, request: v2.UpdateRequest, context: grpc.ServicerContext):
        peer = context.peer()
        logger.debug(f"{self.ecu_id}: update request from {peer=}")
        logger.debug(f"{request=}")

        # return if not listed as target
        found = False
        for ecu in request.ecu:
            if ecu.ecu_id == self.ecu_id:
                found = True
                break
        if not found:
            logger.debug(f"{self.ecu_id}, Update: not listed as update target, abort")
            return v2.UpdateResponse()

        results = v2.UpdateResponse()
        if self._in_update.is_set():
            resp_ecu = v2.UpdateResponseEcu(
                ecu_id=self.ecu_id,
                result=v2.RECOVERABLE,
            )
            results.ecu.append(resp_ecu)
        else:
            logger.debug("start update")
            self._in_update.set()
            asyncio.create_task(self._on_update())

        return results

    async def Status(self, _, context: grpc.ServicerContext):
        peer = context.peer()
        logger.debug(f"{self.ecu_id}: status request from {peer=}")
        if self._rebooting.is_set():
            return v2.StatusResponse()

        result_ecu = v2.StatusResponseEcu(
            ecu_id=self.ecu_id,
            result=v2.NO_FAILURE,
        )

        ecu_status = result_ecu.status
        if self._in_update.is_set():
            ecu_status.status = v2.UPDATING
        else:
            ecu_status.status = v2.SUCCESS

        result = v2.StatusResponse()
        result.ecu.append(result_ecu)
        return result


async def launch_otaclient(ecu_id, ecu_ip, ecu_port):
    server = grpc.aio.server()
    service = MiniOtaClientServiceV2(ecu_id)
    v2_grpc.add_OtaClientServiceServicer_to_server(service, server)

    server.add_insecure_port(f"{ecu_ip}:{ecu_port}")
    await server.start()
    await service_wait_for_termination(server)


async def mainecu_mode(ecu_info_file: str):
    ecu_info = yaml.safe_load(Path(ecu_info_file).read_text())
    ecu_id = ecu_info["ecu_id"]
    ecu_ip = ecu_info["ip_addr"]
    ecu_port = int(ecu_info.get("port", _DEFAULT_PORT))

    logger.info(f"start {ecu_id=} at {ecu_ip}:{ecu_port}")
    await launch_otaclient(ecu_id, ecu_ip, ecu_port)


async def subecu_mode(ecu_info_file: str):
    ecu_info = yaml.safe_load(Path(ecu_info_file).read_text())

    # schedule the servers to the thread pool
    tasks: List[asyncio.Task] = []
    for subecu in ecu_info["secondaries"]:
        ecu_id = subecu["ecu_id"]
        ecu_ip = subecu["ip_addr"]
        ecu_port = int(subecu.get("port", _DEFAULT_PORT))
        logger.info(f"start {ecu_id=} at {ecu_ip}:{ecu_port}")
        tasks.append(asyncio.create_task(launch_otaclient(ecu_id, ecu_ip, ecu_port)))

    await asyncio.gather(*tasks)


async def standalone_mode(args: argparse.Namespace):
    await launch_otaclient("standalone", args.ip, args.port)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="calling main ECU's API",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-c", "--ecu_info", default="test_utils/ecu_info.yaml", help="ecu_info"
    )
    parser.add_argument(
        "mode",
        default="standalone",
        help=(
            "running mode for mini_ota_client(standalone, subecus, mainecu)\n"
            "\tstandalone: run a single mini_ota_client\n"
            "\tmainecu: run a single mini_ota_client as mainecu according to ecu_info.yaml\n"
            "\tsubecus: run subecu(s) according to ecu_info.yaml"
        ),
    )
    parser.add_argument(
        "--ip",
        default="127.0.0.1",
        help="(standalone) listen at IP",
    )
    parser.add_argument(
        "--port",
        default=_DEFAULT_PORT,
        help="(standalone) use port PORT",
    )

    args = parser.parse_args()

    if args.mode not in _MODE:
        parser.error(f"invalid mode {args.mode}, should be one of {_MODE}")
    if args.mode != "standalone" and not Path(args.ecu_info).is_file():
        parser.error(
            f"invalid ecu_info_file {args.ecu_info!r}. ecu_info.yaml is required for non-standalone mode"
        )

    if args.mode == "subecus":
        logger.info("subecus mode")
        coro = subecu_mode(args.ecu_info)
    elif args.mode == "mainecu":
        logger.info("mainecu mode")
        coro = mainecu_mode(args.ecu_info)
    else:
        logger.info("standalone mode")
        coro = standalone_mode(args)

    asyncio.run(coro)
