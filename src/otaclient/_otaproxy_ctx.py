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
"""Control of the otaproxy server startup/shutdown.

The API exposed by this module is meant to be controlled by otaproxy managing thread only.
"""

from __future__ import annotations

import atexit
import logging
import multiprocessing as mp
import multiprocessing.context as mp_ctx
import shutil
import threading
import time
from functools import partial
from pathlib import Path
from typing import Callable

from ota_proxy import run_otaproxy
from otaclient._types import MultipleECUStatusFlags
from otaclient._utils import SharedOTAClientMetricsWriter
from otaclient.configs.cfg import cfg, proxy_info
from otaclient_common.common import ensure_otaproxy_start

logger = logging.getLogger(__name__)

_otaproxy_p: mp_ctx.SpawnProcess | None = None
_global_shutdown = threading.Event()
_global_process_lock = threading.Lock()


def otaproxy_on_global_shutdown() -> None:
    global _global_shutdown
    if _global_shutdown.is_set():
        return

    _global_shutdown.set()
    _shutdown_otaproxy()


def _shutdown_otaproxy():
    global _otaproxy_p
    with _global_process_lock:
        if _otaproxy_p:
            _otaproxy_p.terminate()
            _otaproxy_p.join()
            _otaproxy_p = None


OTAPROXY_CHECK_INTERVAL = 3
OTAPROXY_MIN_STARTUP_TIME = 120
"""Keep otaproxy running at least 60 seconds after startup."""
OTA_CACHE_DIR_CHECK_INTERVAL = 60


def otaproxy_process(
    *,
    init_cache: bool,
    shm_metrics_writer_factory: Callable[[], SharedOTAClientMetricsWriter],
) -> None:
    from otaclient._logging import configure_logging

    configure_logging()
    logger.info("otaproxy process started")

    external_cache_mnt_point = None
    if cfg.OTAPROXY_ENABLE_EXTERNAL_CACHE:
        external_cache_mnt_point = cfg.EXTERNAL_CACHE_DEV_MOUNTPOINT

    external_nfs_cache_mnt_point = None
    if proxy_info.external_nfs_cache_mnt_point:
        external_nfs_cache_mnt_point = str(proxy_info.external_nfs_cache_mnt_point)

    host, port = (
        str(proxy_info.local_ota_proxy_listen_addr),
        proxy_info.local_ota_proxy_listen_port,
    )

    upper_proxy = str(proxy_info.upper_ota_proxy or "")
    logger.info(f"will launch otaproxy at http://{host}:{port}, with {upper_proxy=}")
    if upper_proxy:
        logger.info(f"wait for {upper_proxy=} online...")
        ensure_otaproxy_start(str(upper_proxy))

    shm_metrics_writer = shm_metrics_writer_factory()

    run_otaproxy(
        host=host,
        port=port,
        init_cache=init_cache,
        cache_dir=cfg.OTAPROXY_CACHE_DIR,
        cache_db_f=f"{cfg.OTAPROXY_CACHE_DIR}/cache_db",
        upper_proxy=upper_proxy,
        # NOTE(20250801): Starting from otaclient v3.9.1, we have inplace update mode with OTA resume,
        #                   on child ECU, we don't need to rely on OTA cache to speed up OTA retry anymore.
        # NOTE(20250801): Due to proxy_info.yaml is forced preserved across each OTA(multiple methods are used
        #                   to ensure that, unfortunately), currently there is no way to update the proxy_info.yaml
        #                   file on the ECU via OTA.
        #                 For now, hardcoded to disable OTA cache on the child ECU.
        #                 To be noticed that, otaproxy OTA cache on main ECU is still needed for streaming
        #                   the same requests from multiple child ECUs to reduce duplicate downloads.
        enable_cache=proxy_info.enable_local_ota_proxy_cache
        and proxy_info.upper_ota_proxy is None,
        enable_https=proxy_info.gateway_otaproxy,
        external_cache_mnt_point=external_cache_mnt_point,
        external_nfs_cache_mnt_point=external_nfs_cache_mnt_point,
        shm_metrics_writer=shm_metrics_writer,
    )


def otaproxy_control_thread(
    ecu_status_flags: MultipleECUStatusFlags,
    shm_metrics_writer_factory: Callable[[], SharedOTAClientMetricsWriter],
) -> None:  # pragma: no cover
    atexit.register(otaproxy_on_global_shutdown)

    _mp_ctx = mp.get_context("spawn")

    ota_cache_dir = Path(cfg.OTAPROXY_CACHE_DIR)
    next_ota_cache_dir_checkpoint = 0
    otaproxy_min_alive_until = 0

    global _otaproxy_p
    while not _global_shutdown.is_set():
        time.sleep(OTAPROXY_CHECK_INTERVAL)
        _now = time.time()

        with _global_process_lock:
            _otaproxy_running = _otaproxy_p and _otaproxy_p.is_alive()
        _otaproxy_should_run = ecu_status_flags.any_requires_network.is_set()
        _all_success = ecu_status_flags.all_success.is_set()

        if not _otaproxy_should_run and not _otaproxy_running:
            if (
                _now > next_ota_cache_dir_checkpoint
                and _all_success
                and ota_cache_dir.is_dir()
                and any(ota_cache_dir.iterdir())
            ):
                logger.info(
                    "all tracked ECUs are in SUCCESS OTA status, cleanup ota cache dir ..."
                )
                next_ota_cache_dir_checkpoint = _now + OTA_CACHE_DIR_CHECK_INTERVAL
                shutil.rmtree(ota_cache_dir, ignore_errors=True)

        elif _otaproxy_should_run and not _otaproxy_running:
            # NOTE: always try to re-use cache. If the cache dir is empty, otaproxy
            #   will still init the cache even init_cache is False.
            with _global_process_lock:
                if not _global_shutdown.is_set():
                    _otaproxy_p = _mp_ctx.Process(
                        target=partial(
                            otaproxy_process,
                            init_cache=False,
                            shm_metrics_writer_factory=shm_metrics_writer_factory,
                        ),
                        name="otaproxy",
                    )
                    _otaproxy_p.start()
                    next_ota_cache_dir_checkpoint = otaproxy_min_alive_until = (
                        _now + OTAPROXY_MIN_STARTUP_TIME
                    )

        elif _otaproxy_p and _otaproxy_running and not _otaproxy_should_run:
            if _now > otaproxy_min_alive_until:  # to prevent pre-mature shutdown
                logger.info("shutting down otaproxy as not needed now ...")
                _shutdown_otaproxy()
