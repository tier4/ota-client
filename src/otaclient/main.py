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
import signal
import sys
import threading
import time
from functools import partial
from multiprocessing.queues import Queue as mp_Queue
from pathlib import Path
from typing import NoReturn

from otaclient import __version__
from otaclient.app.configs import config as cfg
from otaclient.app.configs import ecu_info, server_cfg
from otaclient.log_setting import configure_logging
from otaclient.otaproxy import (
    otaproxy_running,
    shutdown_otaproxy_server,
    start_otaproxy_server,
)
from otaclient_common.common import read_str_from_file, write_str_to_file_sync

# configure logging before any code being executed
configure_logging()
logger = logging.getLogger("otaclient")

_ota_server_p: mp_ctx.SpawnProcess | None = None
_ota_core_p: mp_ctx.SpawnProcess | None = None
_otaclient_global_shutdown: mp_sync.Event | None = None


def _global_shutdown():  # pragma: no cover
    global _otaclient_global_shutdown
    if _otaclient_global_shutdown:
        _otaclient_global_shutdown.set()

    # ensure the subprocesses are joined
    if _ota_server_p:
        _ota_server_p.join()
    if _ota_core_p:
        _ota_core_p.join()


atexit.register(_global_shutdown)


def _mainp_signterm_handler(signame, frame) -> NoReturn:
    global _otaclient_global_shutdown
    if _otaclient_global_shutdown:
        _otaclient_global_shutdown.set()

    if _ota_core_p:
        _ota_core_p.terminate()
    if _ota_server_p:
        _ota_server_p.terminate()

    raise KeyboardInterrupt(
        "main receives SIGTERM, terminate subprocesses and exits ..."
    )


def _subp_signterm_handler(signame, frame) -> NoReturn:
    raise KeyboardInterrupt("receives SIGTERM, exits ...")


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


def apiv2_server_main(
    *,
    status_report_queue: mp_Queue,
    operation_push_queue: mp_Queue,
    operation_ack_queue: mp_Queue,
    any_requires_network: mp_sync.Event,
):  # pragma: no cover
    """OTA API server process main.

    NOTE that the imports within this function have side-effect, so we don't
        import them globally.
    """
    import grpc.aio as grpc_aio

    from otaclient.api_v2.ecu_status import ECUStatusStorage, ECUTracker
    from otaclient.api_v2.servicer import OTAClientAPIServicer as APIv2Servicer
    from otaclient_api.v2 import otaclient_v2_pb2_grpc as v2_grpc

    # NOTE: spawn will not let the child process inherits the signal handler
    signal.signal(signal.SIGTERM, _subp_signterm_handler)

    async def _main():
        server = grpc_aio.server()
        server.add_insecure_port(f"{ecu_info.ip_addr}:{server_cfg.SERVER_PORT}")

        ecu_status_storage = ECUStatusStorage(
            any_requires_network=any_requires_network,
        )
        ecu_tracker = ECUTracker(
            ecu_status_storage=ecu_status_storage,
            status_report_queue=status_report_queue,
        )
        ecu_tracker.start_tracking()

        # mount API v2 servicer
        v2_grpc.add_OtaClientServiceServicer_to_server(
            server=server,
            servicer=APIv2Servicer(
                ecu_status_storage=ecu_status_storage,
                operation_push_queue=operation_push_queue,
                operation_ack_queue=operation_ack_queue,
            ),
        )

        logger.info("OTA API server started")
        await server.start()
        try:
            await server.wait_for_termination()
        finally:
            await server.stop(1)

    asyncio.run(_main())


def ota_app_main(
    *,
    status_report_queue: mp_Queue,
    operation_push_queue: mp_Queue,
    operation_ack_queue: mp_Queue,
    reboot_flag: mp_sync.Event,
    global_shutdown_flag: mp_sync.Event,
):  # pragma: no cover
    """Main entry of otaclient app process."""
    from otaclient.ota_app import OTAClientAPP

    signal.signal(signal.SIGTERM, _subp_signterm_handler)

    otaclient_app = OTAClientAPP(
        status_report_queue=status_report_queue,
        operation_push_queue=operation_push_queue,
        operation_ack_queue=operation_ack_queue,
        reboot_flag=reboot_flag,
        global_shutdown_flag=global_shutdown_flag,
    )
    logger.info("otaclient app started")
    otaclient_app.start()


OTAPROXY_CHECK_INTERVAL = 3
OTAPROXY_MIN_STARTUP_TIME = 60
"""Keep otaproxy running at least 60 seconds after startup."""


def otaproxy_control_thread(
    *,
    any_requires_network: mp_sync.Event,
    reboot_flag: mp_sync.Event,
    global_shutdown_flag: mp_sync.Event,
) -> None:  # pragma: no cover
    while not global_shutdown_flag.is_set():
        time.sleep(OTAPROXY_CHECK_INTERVAL)

        _otaproxy_running = otaproxy_running()
        _otaproxy_should_run = any_requires_network.is_set()

        if _otaproxy_should_run:
            reboot_flag.clear()
        else:
            reboot_flag.set()

        # NOTE(20240930): always try to re-use already presented ota-cache dir,
        #   as if the ota-cache dir is not empty, it means that there is high possiblity of
        #   previous failed OTA(s) of this ECU(or its sub ECU(s)).
        if _otaproxy_should_run and not _otaproxy_running:
            start_otaproxy_server(init_cache=False)
            time.sleep(OTAPROXY_MIN_STARTUP_TIME)  # prevent pre-mature shutdown

        elif _otaproxy_running and not _otaproxy_should_run:
            shutdown_otaproxy_server()


def main() -> None:  # pragma: no cover
    signal.signal(signal.SIGTERM, _mainp_signterm_handler)

    logger.info("started")
    logger.info(f"otaclient version: {__version__}")
    logger.info(f"ecu_info.yaml: \n{ecu_info}")

    # start the otaclient grpc server
    _check_other_otaclient()

    ctx = mp.get_context("spawn")

    # flags
    any_requires_network = ctx.Event()
    reboot_flag = ctx.Event()
    status_report_q = ctx.Queue()
    operation_push_q = ctx.Queue()
    operation_ack_q = ctx.Queue()
    otaclient_global_shutdown = ctx.Event()

    global _ota_core_p, _ota_server_p, _ota_operation_q, _ota_ack_q, _otaclient_global_shutdown
    _otaclient_global_shutdown = otaclient_global_shutdown
    _ota_operation_q = operation_push_q
    _ota_ack_q = operation_ack_q

    _ota_core_p = ctx.Process(
        target=partial(
            ota_app_main,
            status_report_queue=status_report_q,
            operation_push_queue=operation_push_q,
            operation_ack_queue=operation_ack_q,
            reboot_flag=reboot_flag,
            global_shutdown_flag=otaclient_global_shutdown,
        ),
    )
    _ota_core_p.start()

    _ota_server_p = ctx.Process(
        target=partial(
            apiv2_server_main,
            status_report_queue=status_report_q,
            operation_push_queue=operation_push_q,
            operation_ack_queue=operation_ack_q,
            any_requires_network=any_requires_network,
        ),
    )
    _ota_server_p.start()

    _otaproxy_control_t = threading.Thread(
        target=partial(
            otaproxy_control_thread,
            any_requires_network=any_requires_network,
            reboot_flag=reboot_flag,
            global_shutdown_flag=otaclient_global_shutdown,
        ),
        daemon=True,
        name="otaclient_otaproxy_control_t",
    )
    _otaproxy_control_t.start()

    _ota_core_p.join()
    _ota_server_p.join()
