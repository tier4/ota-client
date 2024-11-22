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
"""Entrypoint of otaclient."""


from __future__ import annotations

import asyncio
import atexit
import logging
import multiprocessing as mp
import multiprocessing.shared_memory as mp_shm
import multiprocessing.synchronize as mp_sync
import secrets
import shutil
import threading
import time
from functools import partial
from multiprocessing.queues import Queue as mp_Queue
from pathlib import Path
from queue import Queue
from typing import NoReturn

from otaclient import __version__
from otaclient._status_monitor import OTAClientStatusCollector
from otaclient._types import IPCRequest, IPCResponse
from otaclient._utils import SharedOTAClientStatusReader, SharedOTAClientStatusWriter

logger = logging.getLogger(__name__)

HEALTH_CHECK_INTERAVL = 6  # seconds
OTAPROXY_CHECK_INTERVAL = 3
OTAPROXY_MIN_STARTUP_TIME = 60
"""Keep otaproxy running at least 60 seconds after startup."""
OTA_CACHE_DIR_CHECK_INTERVAL = 60
SHUTDOWN_AFTER_CORE_EXIT = 16
SHUTDOWN_AFTER_API_SERVER_EXIT = 3

_global_shutdown: bool = False


def _on_global_shutdown():
    global _global_shutdown
    _global_shutdown = True


def ota_core_process(
    shm_writer_factory,
    control_flag: mp_sync.Event,
    op_queue: mp_Queue[IPCRequest | IPCResponse],
):
    from otaclient._logging import configure_logging
    from otaclient.configs.cfg import proxy_info
    from otaclient.ota_core import OTAClient

    atexit.register(_on_global_shutdown)
    shm_writer = shm_writer_factory()
    atexit.register(shm_writer.atexit)

    configure_logging()

    _local_status_report_queue = Queue()
    _status_monitor = OTAClientStatusCollector(
        msg_queue=_local_status_report_queue,
        shm_status=shm_writer,
    )
    _status_monitor.start()

    _ota_core = OTAClient(
        control_flag=control_flag,
        proxy=proxy_info.get_proxy_for_local_ota(),
        status_report_queue=_local_status_report_queue,
    )
    _ota_core.main(op_queue)


def grpc_server_process(
    shm_reader_factory,
    control_flag: mp_sync.Event,
    op_queue: mp_Queue[IPCRequest | IPCResponse],
    all_ecus_succeeded: mp_sync.Event,
    any_requires_network: mp_sync.Event,
) -> NoReturn:  # type: ignore
    from otaclient._logging import configure_logging

    configure_logging()
    atexit.register(_on_global_shutdown)

    shm_reader = shm_reader_factory()
    atexit.register(shm_reader.atexit)

    async def _grpc_server_launcher():
        import grpc.aio

        from otaclient.configs.cfg import cfg, ecu_info
        from otaclient.grpc.api_v2.ecu_status import ECUStatusStorage
        from otaclient.grpc.api_v2.ecu_tracker import ECUTracker
        from otaclient.grpc.api_v2.servicer import OTAClientAPIServicer
        from otaclient_api.v2 import otaclient_v2_pb2_grpc as v2_grpc
        from otaclient_api.v2.api_stub import OtaClientServiceV2

        ecu_status_storage = ECUStatusStorage(
            all_ecus_succeeded=all_ecus_succeeded,
            any_requires_network=any_requires_network,
        )
        ecu_tracker = ECUTracker(ecu_status_storage, shm_reader)
        ecu_tracker.start()

        api_servicer = OTAClientAPIServicer(
            ecu_status_storage,
            op_queue,
            control_flag=control_flag,
        )
        ota_client_service_v2 = OtaClientServiceV2(api_servicer)

        server = grpc.aio.server()
        v2_grpc.add_OtaClientServiceServicer_to_server(
            server=server, servicer=ota_client_service_v2
        )
        server.add_insecure_port(f"{ecu_info.ip_addr}:{cfg.OTA_API_SERVER_PORT}")

        await server.start()
        try:
            await server.wait_for_termination()
        finally:
            await server.stop(1)

    asyncio.run(_grpc_server_launcher())


def otaproxy_control_thread(
    *,
    any_requires_network: mp_sync.Event,
    all_ecus_succeeded: mp_sync.Event,
) -> None:  # pragma: no cover
    from ota_proxy.config import config
    from otaclient._otaproxy_ctx import (
        otaproxy_running,
        shutdown_otaproxy_server,
        start_otaproxy_server,
    )

    ota_cache_dir = Path(config.BASE_DIR)
    next_ota_cache_dir_checkpoint = 0

    atexit.register(shutdown_otaproxy_server)

    while not _global_shutdown:
        time.sleep(OTAPROXY_CHECK_INTERVAL)

        _otaproxy_running = otaproxy_running()
        _otaproxy_should_run = any_requires_network.is_set()

        if not _otaproxy_should_run and not _otaproxy_running:
            _now = time.time()
            if (
                _now > next_ota_cache_dir_checkpoint
                and all_ecus_succeeded.is_set()
                and ota_cache_dir.is_dir()
            ):
                logger.info(
                    "all tracked ECUs are in SUCCESS OTA status, cleanup ota cache dir ..."
                )
                next_ota_cache_dir_checkpoint = _now + OTA_CACHE_DIR_CHECK_INTERVAL
                shutil.rmtree(ota_cache_dir, ignore_errors=True)

        elif _otaproxy_should_run and not _otaproxy_running:
            start_otaproxy_server(init_cache=False)
            time.sleep(OTAPROXY_MIN_STARTUP_TIME)  # prevent pre-mature shutdown

        elif not _otaproxy_should_run and _otaproxy_running:
            shutdown_otaproxy_server()


STATUS_SHM_SIZE = 4096
SHM_HMAC_KEY_LEN = 64  # bytes


def main() -> None:
    from otaclient._logging import configure_logging
    from otaclient._utils import check_other_otaclient, create_otaclient_rundir
    from otaclient.configs.cfg import cfg, ecu_info, proxy_info

    # configure logging before any code being executed
    configure_logging()

    logger.info("started")
    logger.info(f"otaclient version: {__version__}")
    logger.info(f"ecu_info.yaml: \n{ecu_info}")

    check_other_otaclient(cfg.OTACLIENT_PID_FILE)
    create_otaclient_rundir(cfg.RUN_DIR)

    mp_ctx = mp.get_context("spawn")
    shm = mp_shm.SharedMemory(size=STATUS_SHM_SIZE, create=True)
    _key = secrets.token_bytes(SHM_HMAC_KEY_LEN)
    atexit.register(shm.close)
    atexit.register(shm.unlink)

    # shared queus and flags
    local_otaclient_control_flag = mp_ctx.Event()
    local_otaclient_op_queue = mp_ctx.Queue()
    all_ecus_succeeded = mp_ctx.Event()
    any_requires_network = mp_ctx.Event()

    _ota_core_p = mp_ctx.Process(
        target=partial(
            ota_core_process,
            partial(SharedOTAClientStatusWriter, name=shm.name, key=_key),
            local_otaclient_control_flag,
            local_otaclient_op_queue,
        ),
        name="otaclient_ota_core",
    )
    _ota_core_p.start()

    _grpc_server_p = mp_ctx.Process(
        target=partial(
            grpc_server_process,
            partial(SharedOTAClientStatusReader, name=shm.name, key=_key),
            local_otaclient_control_flag,
            local_otaclient_op_queue,
            all_ecus_succeeded,
            any_requires_network,
        ),
        name="otaclient_api_server",
    )
    _grpc_server_p.start()

    # we only setup the resources in main process
    del _key, local_otaclient_control_flag, local_otaclient_op_queue

    # ------ configuring main process ------ #

    atexit.register(_on_global_shutdown)
    _otaproxy_control_t = None
    if proxy_info.enable_local_ota_proxy:
        _otaproxy_control_t = threading.Thread(
            target=partial(
                otaproxy_control_thread,
                any_requires_network=any_requires_network,
                all_ecus_succeeded=all_ecus_succeeded,
            ),
            daemon=True,
            name="otaclient_otaproxy_control_t",
        )
        _otaproxy_control_t.start()

    while not _global_shutdown:
        time.sleep(HEALTH_CHECK_INTERAVL)

        if not _ota_core_p.is_alive():
            logger.error(
                "ota_core process is dead! "
                f"otaclient will exit in {SHUTDOWN_AFTER_CORE_EXIT}seconds ..."
            )
            time.sleep(SHUTDOWN_AFTER_CORE_EXIT)
            # TODO: shutdown

        if not _grpc_server_p.is_alive():
            logger.error(
                f"ota API server is dead, whole otaclient will exit in {SHUTDOWN_AFTER_API_SERVER_EXIT}seconds ..."
            )
            time.sleep(SHUTDOWN_AFTER_API_SERVER_EXIT)
            # TODO: shutdown
