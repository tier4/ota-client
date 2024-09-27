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
import atexit
import logging
import multiprocessing as mp
import multiprocessing.context as mp_ctx
import multiprocessing.synchronize as mp_sync
import os
import sys
from pathlib import Path

from otaclient import __version__
from otaclient._types import OTAOperationResp
from otaclient.app.configs import config as cfg
from otaclient.app.configs import ecu_info, server_cfg
from otaclient.log_setting import configure_logging
from otaclient_common.common import read_str_from_file, write_str_to_file_sync

# configure logging before any code being executed
configure_logging()
logger = logging.getLogger("otaclient")

_ota_server_p: mp_ctx.SpawnProcess | None = None
_ota_core_p: mp_ctx.SpawnProcess | None = None
_operation_ack_q: mp.Queue[OTAOperationResp] | None = None


def _global_shutdown():  # pragma: no cover
    if _ota_server_p:
        _ota_server_p.join()
    if _ota_core_p:
        _ota_core_p.join()
    if _operation_ack_q:  # to unblock the API handler
        _operation_ack_q.put_nowait(None)  # type: ignore


atexit.register(_global_shutdown)


def _check_other_otaclient():
    """Check if there is another otaclient instance running."""
    # create a lock file to prevent multiple ota-client instances start
    if pid := read_str_from_file(cfg.OTACLIENT_PID_FILE):
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
    write_str_to_file_sync(cfg.OTACLIENT_PID_FILE, f"{os.getpid()}")


def api_server_main(
    *,
    status_report_queue: mp.Queue,
    operation_push_queue: mp.Queue,
    operation_ack_queue: mp.Queue,
    reboot_flag: mp_sync.Event,
):  # pragma: no cover
    """OTA API server process main.

    NOTE that the imports within this function have side-effect, so we don't
        import them globally.
    """
    import grpc.aio as grpc_aio

    from otaclient.api_v2.servicer import OTAClientAPIServicer as APIv2Servicer
    from otaclient_api.v2 import otaclient_v2_pb2_grpc as v2_grpc

    async def _main():
        server = grpc_aio.server()
        server.add_insecure_port(f"{ecu_info.ip_addr}:{server_cfg.SERVER_PORT}")

        # mount API v2 servicer
        v2_grpc.add_OtaClientServiceServicer_to_server(
            server=server,
            servicer=APIv2Servicer(
                status_report_queue=status_report_queue,
                operation_push_queue=operation_push_queue,
                operation_ack_queue=operation_ack_queue,
                reboot_flag=reboot_flag,
            ),
        )

        logger.info("OTA API server started")
        await server.start()
        await server.wait_for_termination()

    asyncio.run(_main())


def ota_app_main(
    *,
    status_report_queue: mp.Queue,
    operation_push_queue: mp.Queue,
    operation_ack_queue: mp.Queue,
    reboot_flag: mp_sync.Event,
):  # pragma: no cover
    """Main entry of otaclient app process."""
    from otaclient.ota_app import OTAClientAPP

    otaclient_app = OTAClientAPP(
        status_report_queue=status_report_queue,
        operation_push_queue=operation_push_queue,
        operation_ack_queue=operation_ack_queue,
        reboot_flag=reboot_flag,
    )
    logger.info("otaclient app started")
    otaclient_app.main()


def main() -> None:  # pragma: no cover
    logger.info("started")
    logger.info(f"otaclient version: {__version__}")
    logger.info(f"ecu_info.yaml: \n{ecu_info}")

    # start the otaclient grpc server
    _check_other_otaclient()

    ctx = mp.get_context("spawn")

    global _operation_ack_q
    ipc_primitives = {
        "status_report_queue": ctx.Queue(),
        "operation_push_queue": ctx.Queue(),
        "operation_ack_queue": (_operation_ack_q := ctx.Queue()),
        "reboot_flag": ctx.Event(),
    }

    global _ota_core_p, _ota_server_p
    _ota_core_p = ctx.Process(target=ota_app_main, kwargs=ipc_primitives)
    _ota_server_p = ctx.Process(target=api_server_main, kwargs=ipc_primitives)

    _ota_core_p.start()
    _ota_server_p.start()

    _ota_core_p.join()
    _ota_server_p.join()
