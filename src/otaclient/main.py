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

import atexit
import logging
import multiprocessing as mp
import multiprocessing.context as mp_ctx
import multiprocessing.synchronize as mp_sync
import shutil
import signal
import threading
import time
from functools import partial
from multiprocessing.queues import Queue as mp_Queue
from pathlib import Path
from typing import NoReturn

import otaclient
from otaclient.configs.cfg import proxy_info
from otaclient.grpc.main import apiv2_server_main
from otaclient.log_setting import configure_logging
from otaclient.otaproxy import (
    otaproxy_running,
    shutdown_otaproxy_server,
    start_otaproxy_server,
)

logger = logging.getLogger(__name__)

_ota_server_p: mp_ctx.SpawnProcess | None = None
_ota_core_p: mp_ctx.SpawnProcess | None = None
_main_global_shutdown_flag: mp_sync.Event | None = None


def _global_shutdown():  # pragma: no cover
    if _main_global_shutdown_flag:
        _main_global_shutdown_flag.set()

    # ensure the subprocesses are joined
    if _ota_server_p:
        _ota_server_p.join()
    if _ota_core_p:
        _ota_core_p.join()


atexit.register(_global_shutdown)


def _mainp_signterm_handler(signame, frame) -> NoReturn:
    """Terminate all the subprocess and then raise KeyboardInterrupt."""

    if _main_global_shutdown_flag:
        _main_global_shutdown_flag.set()

    if _ota_core_p:
        _ota_core_p.terminate()
        _ota_core_p.join()
    if _ota_server_p:
        _ota_server_p.terminate()
        _ota_server_p.join()

    raise KeyboardInterrupt(
        "main receives SIGTERM, terminate subprocesses and exits ..."
    )


def _subp_signterm_handler(signame, frame) -> NoReturn:
    raise KeyboardInterrupt("receives SIGTERM, exits ...")


def ota_app_main(
    *,
    status_report_queue: mp_Queue,
    operation_push_queue: mp_Queue,
    operation_ack_queue: mp_Queue,
    reboot_flag: mp_sync.Event,
    global_shutdown_flag: mp_sync.Event,
):  # pragma: no cover
    """Main entry of otaclient app process."""
    import otaclient
    from otaclient.ota_app import OTAClientAPP

    # logging needed to be configured again at new process
    configure_logging()

    global _main_global_shutdown_flag
    _main_global_shutdown_flag = global_shutdown_flag

    otaclient._global_shutdown_flag = global_shutdown_flag
    signal.signal(signal.SIGTERM, _subp_signterm_handler)

    otaclient_app = OTAClientAPP(
        status_report_queue=status_report_queue,
        operation_push_queue=operation_push_queue,
        operation_ack_queue=operation_ack_queue,
        reboot_flag=reboot_flag,
    )
    logger.info("otaclient app started")
    otaclient_app.start()


OTAPROXY_CHECK_INTERVAL = 3
OTAPROXY_MIN_STARTUP_TIME = 60
"""Keep otaproxy running at least 60 seconds after startup."""
OTA_CACHE_DIR_CHECK_INTERVAL = 60


def otaproxy_control_thread(
    *,
    any_requires_network: mp_sync.Event,
    all_ecus_succeeded: mp_sync.Event,
) -> None:  # pragma: no cover
    from ota_proxy.config import config

    # TODO: use the otaproxy base_dir config from app.config
    ota_cache_dir = Path(config.BASE_DIR)
    next_ota_cache_dir_checkpoint = 0
    while not otaclient.global_shutdown():
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
                shutil.rmtree(ota_cache_dir)

        elif _otaproxy_should_run and not _otaproxy_running:
            start_otaproxy_server(init_cache=False)
            time.sleep(OTAPROXY_MIN_STARTUP_TIME)  # prevent pre-mature shutdown

        elif not _otaproxy_should_run and _otaproxy_running:
            shutdown_otaproxy_server()


SHUTDOWN_AFTER_CORE_EXIT = 45
"""Shutdown the whole otaclient after 45 seconds when ota_core process exits.

This gives the API server chance to report failure info via status API.
"""
SHUTDOWN_AFTER_API_SERVER_EXIT = 6
HEALTH_CHECK_INTERAVL = 6


def main() -> None:  # pragma: no cover
    """The main entry of otaclient."""
    signal.signal(signal.SIGTERM, _mainp_signterm_handler)

    ctx = mp.get_context("spawn")
    global_shutdown_flag = ctx.Event()
    global _main_global_shutdown_flag
    _main_global_shutdown_flag = global_shutdown_flag
    otaclient._global_shutdown_flag = global_shutdown_flag

    # ota_core <-> ota API server
    no_child_ecus_in_update = ctx.Event()
    status_report_q = ctx.Queue()
    operation_push_q = ctx.Queue()
    operation_ack_q = ctx.Queue()

    # otaproxy_control <-> ota API server
    all_ecus_succeeded = ctx.Event()
    any_requires_network = ctx.Event()

    global _ota_core_p, _ota_server_p, _ota_operation_q, _ota_ack_q
    _ota_operation_q = operation_push_q
    _ota_ack_q = operation_ack_q

    _ota_core_p = ctx.Process(
        target=partial(
            ota_app_main,
            status_report_queue=status_report_q,
            operation_push_queue=operation_push_q,
            operation_ack_queue=operation_ack_q,
            # NOTE(20241003): currently the allow reboot condition is there is
            #   no ECU in update. In the future consider to release the condition
            #   to no ECU requires otaproxy.
            reboot_flag=no_child_ecus_in_update,
            global_shutdown_flag=global_shutdown_flag,
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
            global_shutdown_flag=global_shutdown_flag,
            no_child_ecus_in_update=no_child_ecus_in_update,
            all_ecus_succeeded=all_ecus_succeeded,
        ),
    )
    _ota_server_p.start()

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

    while not _main_global_shutdown_flag.is_set():
        time.sleep(HEALTH_CHECK_INTERAVL)

        if not _ota_core_p.is_alive():
            logger.error(
                f"ota_core process is dead, otaclient will exit in {SHUTDOWN_AFTER_CORE_EXIT}seconds ..."
            )
            time.sleep(SHUTDOWN_AFTER_CORE_EXIT)
            _mainp_signterm_handler(None, None)  # directly use the signterm handler

        if not _ota_server_p.is_alive():
            logger.error(
                f"ota API server is dead, whole otaclient will exit in {SHUTDOWN_AFTER_API_SERVER_EXIT}seconds ..."
            )
            time.sleep(SHUTDOWN_AFTER_API_SERVER_EXIT)
            _mainp_signterm_handler(None, None)  # directly use the signterm handler
