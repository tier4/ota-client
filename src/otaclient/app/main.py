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


from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import grpc.aio

from otaclient import __version__
from otaclient.app.configs import config as cfg
from otaclient.app.configs import ecu_info, server_cfg
from otaclient.grpc.api_v2.servicer import OTAClientAPIServicer
from otaclient.log_setting import configure_logging
from otaclient_api.v2 import otaclient_v2_pb2_grpc as v2_grpc
from otaclient_api.v2.api_stub import OtaClientServiceV2
from otaclient_common._io import read_str_from_file, write_str_to_file_atomic

# configure logging before any code being executed
configure_logging()
logger = logging.getLogger(__name__)


def _check_other_otaclient():
    """Check if there is another otaclient instance running."""
    # create a lock file to prevent multiple ota-client instances start
    if pid := read_str_from_file(cfg.OTACLIENT_PID_FILE, _default=""):
        # running process will have a folder under /proc
        if Path(f"/proc/{pid}").is_dir():
            logger.error(f"another instance of ota-client({pid=}) is running, abort")
            sys.exit()
        else:
            logger.warning(f"dangling otaclient lock file({pid=}) detected, cleanup")
            Path(cfg.OTACLIENT_PID_FILE).unlink(missing_ok=True)
    # create run dir
    _run_dir = Path(cfg.RUN_DIR)
    _run_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(_run_dir, 0o550)
    # write our pid to the lock file
    write_str_to_file_atomic(cfg.OTACLIENT_PID_FILE, f"{os.getpid()}")


def create_otaclient_grpc_server():
    service_stub = OTAClientAPIServicer()
    ota_client_service_v2 = OtaClientServiceV2(service_stub)

    server = grpc.aio.server()
    v2_grpc.add_OtaClientServiceServicer_to_server(
        server=server, servicer=ota_client_service_v2
    )
    server.add_insecure_port(f"{ecu_info.ip_addr}:{server_cfg.SERVER_PORT}")
    return server


async def launch_otaclient_grpc_server():
    server = create_otaclient_grpc_server()
    await server.start()
    await server.wait_for_termination()


def main():
    logger.info("started")
    logger.info(f"otaclient version: {__version__}")
    logger.info(f"ecu_info.yaml: \n{ecu_info}")

    # start the otaclient grpc server
    _check_other_otaclient()
    asyncio.run(launch_otaclient_grpc_server())
