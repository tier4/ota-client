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
import multiprocessing.resource_tracker as mp_resource_tracker
import multiprocessing.shared_memory as mp_shm
import os
import secrets
import signal
import sys
import threading
import time
from functools import partial

from otaclient import __version__
from otaclient._types import ClientUpdateControlFlags, MultipleECUStatusFlags
from otaclient._utils import SharedOTAClientStatusReader, SharedOTAClientStatusWriter

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


def _on_shutdown(sys_exit: bool = False) -> None:  # pragma: no cover
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

    if sys_exit:
        sys.exit(1)


def _signal_handler(signal_value, _) -> None:  # pragma: no cover
    print(f"otaclient receives {signal_value=}, shutting down ...")
    # NOTE: the daemon_process needs to exit also.
    _on_shutdown(sys_exit=True)


def main() -> None:  # pragma: no cover
    from otaclient._logging import configure_logging
    from otaclient._otaproxy_ctx import (
        otaproxy_control_thread,
        otaproxy_on_global_shutdown,
    )
    from otaclient._utils import check_other_otaclient, create_otaclient_rundir
    from otaclient.client_package import OTAClientPackagePrepareter
    from otaclient.configs.cfg import cfg, ecu_info, proxy_info
    from otaclient.grpc.api_v2.main import grpc_server_process
    from otaclient.ota_core import ota_core_process
    from otaclient_common import _env

    # configure logging before any code being executed
    configure_logging()

    logger.info("started")
    logger.info(f"otaclient started with {sys.executable=}, {sys.argv=}")
    logger.info(f"pid: {os.getpid()}")
    logger.info(f"otaclient version: {__version__}")
    logger.info(f"ecu_info.yaml: \n{ecu_info}")
    logger.info(f"proxy_info.yaml: \n{proxy_info}")
    logger.info(
        f"env.preparing_downloaded_dynamic_ota_client: {os.getenv(cfg.PREPARING_DOWNLOADED_DYNAMIC_OTA_CLIENT)}"
    )
    logger.info(
        f"env.running_downloaded_dynamic_ota_client: {os.getenv(cfg.RUNNING_DOWNLOADED_DYNAMIC_OTA_CLIENT)}"
    )

    if _env.is_dynamic_client_preparing():
        logger.info("preparing downloaded dynamic ota client ...")
        try:
            logger.info("mounting dynamic client squashfs ...")
            client_package_prepareter = OTAClientPackagePrepareter()
            client_package_prepareter.mount_client_package()

            _mount_base = cfg.DYNAMIC_CLIENT_MNT
            logger.info(f"changing root to {_mount_base}")
            os.chroot(_mount_base)
            os.chdir("/")

            logger.info("execve for dynamic client runnning ...")
            running_env = os.environ.copy()
            del running_env[cfg.PREPARING_DOWNLOADED_DYNAMIC_OTA_CLIENT]
            running_env[cfg.RUNNING_DOWNLOADED_DYNAMIC_OTA_CLIENT] = "yes"
            # the process should finish after this execve call
            DYNAMIC_CLIENT_PYTHON_PATH = "/otaclient/venv/bin/python3"
            os.execve(
                path=DYNAMIC_CLIENT_PYTHON_PATH,
                argv=[DYNAMIC_CLIENT_PYTHON_PATH, "-m", "otaclient"],
                env=running_env,
            )
        except Exception as e:
            logger.exception(f"Failed during dynamic client preparation: {e}")
        return _on_shutdown(sys_exit=True)

    if not _env.is_dynamic_client_running():
        # in dynamic client, the pid file has already been created
        check_other_otaclient(cfg.OTACLIENT_PID_FILE)
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
        notify_data_ready_event=mp_ctx.Event(),
        request_shutdown_event=mp_ctx.Event(),
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
        ),
        name="otaclient_api_server",
    )
    _grpc_server_p.start()

    del _key

    # ------ setup main process ------ #

    _otaproxy_control_t = None
    if proxy_info.enable_local_ota_proxy:
        _otaproxy_control_t = threading.Thread(
            target=partial(otaproxy_control_thread, ecu_status_flags),
            daemon=True,
            name="otaclient_otaproxy_control_t",
        )
        _otaproxy_control_t.start()

    while True:
        time.sleep(HEALTH_CHECK_INTERVAL)

        if not _ota_core_p.is_alive():
            logger.error(
                "ota_core process is dead! "
                f"otaclient will exit in {SHUTDOWN_AFTER_CORE_EXIT}seconds ..."
            )
            time.sleep(SHUTDOWN_AFTER_CORE_EXIT)
            return _on_shutdown(sys_exit=True)

        if not _grpc_server_p.is_alive():
            logger.error(
                f"ota API server is dead, whole otaclient will exit in {SHUTDOWN_AFTER_API_SERVER_EXIT}seconds ..."
            )
            time.sleep(SHUTDOWN_AFTER_API_SERVER_EXIT)
            return _on_shutdown(sys_exit=True)

        # launch the dynamic client preparation process
        if client_update_control_flags.notify_data_ready_event.is_set():
            try:
                # kill ota proxy thread if it is running
                if _otaproxy_control_t and _otaproxy_control_t.is_alive():
                    logger.info("killing otaproxy control thread ...")
                    otaproxy_on_global_shutdown()
                    _otaproxy_control_t.join()
                # kill other resources except main process
                logger.info("on main shutdown...")
                _on_shutdown(sys_exit=False)

                logger.info("cleaning up resources ...")
                # this is a python bug(https://github.com/python/cpython/issues/88887),
                # and it is fixed since python3.12 (https://github.com/python/cpython/pull/131530).
                if sys.version_info < (3, 12):
                    _resource_tracker = getattr(
                        mp_resource_tracker, "_resource_tracker", None
                    )
                    if _resource_tracker and hasattr(_resource_tracker, "_stop"):
                        try:
                            _resource_tracker._stop()
                        except Exception as e:
                            logger.error(f"failed to stop the resource tracker: {e!r}")

                logger.info("execve for dynamic client preparation ...")
                # Create a copy of the current environment and modify it
                preparing_env = os.environ.copy()
                preparing_env[cfg.PREPARING_DOWNLOADED_DYNAMIC_OTA_CLIENT] = "yes"
                # Execute with the modified environment
                os.execve(
                    path=sys.executable,
                    argv=[sys.executable, "-m", "otaclient"],
                    env=preparing_env,
                )
            except Exception as e:
                logger.exception(f"Failed during dynamic client preparation: {e}")
                return _on_shutdown(sys_exit=True)

        # shutdown request
        if client_update_control_flags.request_shutdown_event.is_set():
            return _on_shutdown(sys_exit=True)
