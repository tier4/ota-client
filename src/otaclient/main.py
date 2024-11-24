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

import atexit
import logging
import multiprocessing as mp
import multiprocessing.context as mp_ctx
import multiprocessing.shared_memory as mp_shm
import secrets
import signal
import sys
import threading
import time
from functools import partial

from otaclient import __version__
from otaclient._utils import SharedOTAClientStatusReader, SharedOTAClientStatusWriter

logger = logging.getLogger(__name__)

HEALTH_CHECK_INTERAVL = 6  # seconds
SHUTDOWN_AFTER_CORE_EXIT = 16  # seconds
SHUTDOWN_AFTER_API_SERVER_EXIT = 3  # seconds

STATUS_SHM_SIZE = 4096  # bytes
SHM_HMAC_KEY_LEN = 64  # bytes

_ota_core_p: mp_ctx.SpawnProcess | None = None
_grpc_server_p: mp_ctx.SpawnProcess | None = None
_shm: mp_shm.SharedMemory | None = None


def _on_shutdown() -> None:
    global _ota_core_p, _grpc_server_p, _shm
    if _ota_core_p:
        _ota_core_p.terminate()
        _ota_core_p.join()
        _ota_core_p = None

    if _grpc_server_p:
        _grpc_server_p.terminate()
        _grpc_server_p.join()
        _grpc_server_p = None

    if _shm:
        _shm.close()
        _shm.unlink()
        _shm = None


def _signal_handler(signame, _) -> None:
    print(f"otaclient receives {signame=}, shutting down ...")
    _on_shutdown()
    sys.exit(1)


def main() -> None:
    from otaclient._logging import configure_logging
    from otaclient._otaproxy_ctx import otaproxy_control_thread
    from otaclient._utils import check_other_otaclient, create_otaclient_rundir
    from otaclient.configs.cfg import cfg, ecu_info, proxy_info
    from otaclient.grpc.api_v2.main import grpc_server_process
    from otaclient.ota_core import ota_core_process

    # configure logging before any code being executed
    configure_logging()

    logger.info("started")
    logger.info(f"otaclient version: {__version__}")
    logger.info(f"ecu_info.yaml: \n{ecu_info}")

    check_other_otaclient(cfg.OTACLIENT_PID_FILE)
    create_otaclient_rundir(cfg.RUN_DIR)

    #
    # ------ start each processes ------ #
    #
    global _ota_core_p, _grpc_server_p, _shm

    atexit.register(_on_shutdown)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    mp_ctx = mp.get_context("spawn")
    _shm = mp_shm.SharedMemory(size=STATUS_SHM_SIZE, create=True)
    _key = secrets.token_bytes(SHM_HMAC_KEY_LEN)

    # shared queues and flags
    local_otaclient_control_flag = mp_ctx.Event()
    local_otaclient_op_queue = mp_ctx.Queue()
    local_otaclient_resp_queue = mp_ctx.Queue()
    all_ecus_succeeded = mp_ctx.Event()
    any_requires_network = mp_ctx.Event()

    _ota_core_p = mp_ctx.Process(
        target=partial(
            ota_core_process,
            partial(SharedOTAClientStatusWriter, name=_shm.name, key=_key),
            local_otaclient_control_flag,
            local_otaclient_op_queue,
            local_otaclient_resp_queue,
        ),
        name="otaclient_ota_core",
    )
    _ota_core_p.start()

    _grpc_server_p = mp_ctx.Process(
        target=partial(
            grpc_server_process,
            partial(SharedOTAClientStatusReader, name=_shm.name, key=_key),
            local_otaclient_control_flag,
            local_otaclient_op_queue,
            local_otaclient_resp_queue,
            all_ecus_succeeded,
            any_requires_network,
        ),
        name="otaclient_api_server",
    )
    _grpc_server_p.start()

    del _key

    # ------ setup main process ------ #

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

    while True:
        time.sleep(HEALTH_CHECK_INTERAVL)

        if not _ota_core_p.is_alive():
            logger.error(
                "ota_core process is dead! "
                f"otaclient will exit in {SHUTDOWN_AFTER_CORE_EXIT}seconds ..."
            )
            time.sleep(SHUTDOWN_AFTER_CORE_EXIT)
            _on_shutdown()

        if not _grpc_server_p.is_alive():
            logger.error(
                f"ota API server is dead, whole otaclient will exit in {SHUTDOWN_AFTER_API_SERVER_EXIT}seconds ..."
            )
            time.sleep(SHUTDOWN_AFTER_API_SERVER_EXIT)
            _on_shutdown()
