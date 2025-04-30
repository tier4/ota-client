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
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from functools import partial

from otaclient import __version__
from otaclient._types import ClientUpdateControlFlags, MultipleECUStatusFlags
from otaclient._utils import SharedOTAClientStatusReader, SharedOTAClientStatusWriter
from otaclient.configs.cfg import cfg, ecu_info, proxy_info
from otaclient_common.linux import subprocess_popen_wrapper

logger = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 6  # seconds
# NOTE: the reason to let daemon_process exits after 16 seconds of ota_core dead
#   is to allow grpc API server to respond to the status API calls with up-to-date
#   failure information from ota_core.
SHUTDOWN_AFTER_CORE_EXIT = 16  # seconds
SHUTDOWN_AFTER_API_SERVER_EXIT = 3  # seconds

STATUS_SHM_SIZE = 4096  # bytes
MAX_TRACEBACK_SIZE = 2048  # bytes
SHM_HMAC_KEY_LEN = 64  # bytes

_ota_core_p: mp_ctx.SpawnProcess | None = None
_grpc_server_p: mp_ctx.SpawnProcess | None = None
_shm: mp_shm.SharedMemory | None = None
_dynamic_client_p: subprocess.Popen | None = None
_shutdown_processing = threading.Lock()


def _on_shutdown(sys_exit: bool = False) -> None:  # pragma: no cover
    global _shutdown_processing, _dynamic_client_p, _ota_core_p, _grpc_server_p, _shm

    _shutdown_processing.acquire(blocking=False)

    # kill the dynamic client process if it is running
    if _dynamic_client_p and _dynamic_client_p.poll() is None:
        try:
            os.killpg(os.getpgid(_dynamic_client_p.pid), signal.SIGTERM)
        except Exception as e:
            print(f"Failed to kill dynamic client process group: {e}")
        _dynamic_client_p.wait()
        _dynamic_client_p = None

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

    if sys_exit:
        sys.exit(1)


def _signal_handler(signal_value, _) -> None:  # pragma: no cover
    print(f"otaclient receives {signal_value=}, shutting down ...")
    # NOTE: the daemon_process needs to exit also.
    _on_shutdown(sys_exit=True)


def _dynamic_client_shutdown() -> None:
    global _dynamic_client_p
    if _dynamic_client_p and _dynamic_client_p.poll() is None:
        try:
            os.killpg(os.getpgid(_dynamic_client_p.pid), signal.SIGTERM)
        except Exception as e:
            print(f"Failed to kill dynamic client process group: {e}")
        _dynamic_client_p.wait()
        _dynamic_client_p = None
    logger.info("Dynamic client shutdown completed.")


def _dynamic_client_thread(
    client_update_control_flags: ClientUpdateControlFlags,
) -> None:
    atexit.register(_dynamic_client_shutdown)

    try:
        _mount_point = cfg.DYNAMIC_CLIENT_MNT
        if not os.path.exists(_mount_point):
            logger.error(f"Mount dir {_mount_point} does not exist, aborting...")
            raise FileNotFoundError(
                f"Mount dir {_mount_point} does not exist, aborting..."
            )

        # Create a copy of the current environment
        env = os.environ.copy()
        # Add the RUNNING_DOWNLOADED_DYNAMIC_OTA_CLIENT environment variable to hand over to the
        # downloaded OTA client
        env[cfg.RUNNING_DOWNLOADED_DYNAMIC_OTA_CLIENT] = "true"

        # Run the OTA client
        # retry to start the OTA client multiple times if it fails
        logger.info(f"starting dynamic OTA client with mount dir: {_mount_point}")
        for _ in range(cfg.CLIENT_WAKEUP_RETRY_MAX):
            global _shutdown_processing, _dynamic_client_p
            if _shutdown_processing.locked():
                logger.info("shutdown has already been requested, exiting thread...")
                return

            _cmd = [
                "/otaclient/venv/bin/python3",
                "-m",
                "otaclient",
            ]
            _dynamic_client_p = subprocess_popen_wrapper(
                _cmd,
                check_output=True,
                chroot=_mount_point,
                env=env,
                start_new_session=True,
            )
            _dynamic_client_p.wait()
            logger.warning("OTA client exited with non-zero status, restarting...")

        logger.warning(
            "Reached maximum number of retries to start OTA client, shutting down..."
        )
        # If the OTA client fails to start after multiple retries, set the shutdown event
        client_update_control_flags.request_shutdown_event.set()
    except Exception as e:
        logger.exception(f"Failed to start OTA client: {e}")
        client_update_control_flags.request_shutdown_event.set()
    finally:
        _dynamic_client_shutdown()


def main() -> None:  # pragma: no cover
    from otaclient._logging import configure_logging
    from otaclient._otaproxy_ctx import otaproxy_control_thread
    from otaclient._utils import check_other_otaclient, create_otaclient_rundir
    from otaclient.grpc.api_v2.main import grpc_server_process
    from otaclient.ota_core import ota_core_process

    # configure logging before any code being executed
    configure_logging()

    logger.info("started")
    logger.info(f"otaclient version: {__version__}")
    logger.info(f"ecu_info.yaml: \n{ecu_info}")
    logger.info(f"proxy_info.yaml: \n{proxy_info}")
    logger.info(
        f"env.running_downloaded_dynamic_ota_client: {os.getenv(cfg.RUNNING_DOWNLOADED_DYNAMIC_OTA_CLIENT)}"
    )

    check_other_otaclient(
        cfg.OTACLIENT_PID_FILE,
        bool(os.getenv(cfg.RUNNING_DOWNLOADED_DYNAMIC_OTA_CLIENT)),
    )
    create_otaclient_rundir(cfg.RUN_DIR)

    #
    # ------ start each processes ------ #
    #
    global _ota_core_p, _grpc_server_p, _shm

    # NOTE: if the atexit hook is triggered by signal received,
    #   first the signal handler will be executed, and then atexit hook.
    #   At the time atexit hook is executed, the _ota_core_p, _grpc_server_p
    #   and _shm are set to None by signal handler.
    atexit.register(_on_shutdown)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    mp_ctx = mp.get_context("spawn")
    _shm = mp_shm.SharedMemory(size=STATUS_SHM_SIZE, create=True)
    _key = secrets.token_bytes(SHM_HMAC_KEY_LEN)

    # shared queues and flags
    local_otaclient_op_queue = mp_ctx.Queue()
    local_otaclient_resp_queue = mp_ctx.Queue()
    ecu_status_flags = MultipleECUStatusFlags(
        any_child_ecu_in_update=mp_ctx.Event(),
        any_requires_network=mp_ctx.Event(),
        all_success=mp_ctx.Event(),
    )
    client_update_control_flags = ClientUpdateControlFlags(
        stop_server_event=mp_ctx.Event(),
        request_shutdown_event=mp_ctx.Event(),
        start_dynamic_client_event=mp_ctx.Event(),
    )

    _ota_core_p = mp_ctx.Process(
        target=partial(
            ota_core_process,
            shm_writer_factory=partial(
                SharedOTAClientStatusWriter, name=_shm.name, key=_key
            ),
            ecu_status_flags=ecu_status_flags,
            op_queue=local_otaclient_op_queue,
            resp_queue=local_otaclient_resp_queue,
            max_traceback_size=MAX_TRACEBACK_SIZE,
            client_update_control_flags=client_update_control_flags,
        ),
        name="otaclient_ota_core",
    )
    _ota_core_p.start()

    _grpc_server_p = mp_ctx.Process(
        target=partial(
            grpc_server_process,
            shm_reader_factory=partial(
                SharedOTAClientStatusReader, name=_shm.name, key=_key
            ),
            op_queue=local_otaclient_op_queue,
            resp_queue=local_otaclient_resp_queue,
            ecu_status_flags=ecu_status_flags,
            client_update_control_flags=client_update_control_flags,
            should_load_state=bool(
                os.getenv(cfg.RUNNING_DOWNLOADED_DYNAMIC_OTA_CLIENT)
            ),
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
                otaproxy_control_thread, ecu_status_flags, client_update_control_flags
            ),
            daemon=True,
            name="otaclient_otaproxy_control_t",
        )
        _otaproxy_control_t.start()

    _otaclient_dynamic_client_t = None
    while True:
        time.sleep(HEALTH_CHECK_INTERVAL)

        if not _ota_core_p.is_alive():
            logger.error(
                "ota_core process is dead! "
                f"otaclient will exit in {SHUTDOWN_AFTER_CORE_EXIT}seconds ..."
            )
            time.sleep(SHUTDOWN_AFTER_CORE_EXIT)
            return _on_shutdown()

        if not _grpc_server_p.is_alive():
            logger.error(
                f"ota API server is dead, whole otaclient will exit in {SHUTDOWN_AFTER_API_SERVER_EXIT}seconds ..."
            )
            time.sleep(SHUTDOWN_AFTER_API_SERVER_EXIT)
            return _on_shutdown()

        if client_update_control_flags.request_shutdown_event.is_set():
            return _on_shutdown()

        if (
            _otaclient_dynamic_client_t is None
            and client_update_control_flags.start_dynamic_client_event.is_set()
        ):
            logger.info("request to start a new client")
            client_update_control_flags.start_dynamic_client_event.clear()

            _otaclient_dynamic_client_t = threading.Thread(
                target=partial(
                    _dynamic_client_thread,
                    client_update_control_flags,
                ),
                daemon=True,
                name="otaclient_dynamic_client_t",
            )
            _otaclient_dynamic_client_t.start()
            logger.info(
                f"dynamic client thread started with PID: {_otaclient_dynamic_client_t.ident}"
            )
            # wait for the thread to finish
            _otaclient_dynamic_client_t.join()
            _otaclient_dynamic_client_t = None
            logger.info("dynamic client thread exited")
            return _on_shutdown()
