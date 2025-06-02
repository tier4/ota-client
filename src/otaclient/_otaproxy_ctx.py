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
import contextlib
import logging
import multiprocessing as mp
import multiprocessing.context as mp_ctx
import shutil
import threading
import time
from functools import partial
from pathlib import Path

from ota_proxy import config as local_otaproxy_cfg
from ota_proxy import run_otaproxy
from ota_proxy.config import config as otaproxy_cfg
from otaclient._types import ClientUpdateControlFlags, MultipleECUStatusFlags
from otaclient.configs.cfg import cfg, proxy_info
from otaclient_common.common import ensure_otaproxy_start

logger = logging.getLogger(__name__)

_otaproxy_p: mp_ctx.SpawnProcess | None = None
_global_shutdown: bool = False
_global_shutdown_lock = threading.Lock()


def _on_global_shutdown() -> None:
    global _global_shutdown
    _global_shutdown = True
    _shutdown_otaproxy()


def _shutdown_otaproxy():
    global _otaproxy_p
    if _global_shutdown_lock.acquire(blocking=False):
        try:
            if _otaproxy_p:
                _otaproxy_p.terminate()
                _otaproxy_p.join()
                _otaproxy_p = None
        finally:
            _global_shutdown_lock.release()


OTAPROXY_CHECK_INTERVAL = 3
OTAPROXY_MIN_STARTUP_TIME = 120
"""Keep otaproxy running at least 60 seconds after startup."""
OTA_CACHE_DIR_CHECK_INTERVAL = 60


def otaproxy_process(*, init_cache: bool) -> None:
    from otaclient._logging import configure_logging

    configure_logging()
    logger.info("otaproxy process started")

    external_cache_mnt_point = None
    if cfg.OTAPROXY_ENABLE_EXTERNAL_CACHE:
        external_cache_mnt_point = cfg.EXTERNAL_CACHE_DEV_MOUNTPOINT

    host, port = (
        str(proxy_info.local_ota_proxy_listen_addr),
        proxy_info.local_ota_proxy_listen_port,
    )

    upper_proxy = str(proxy_info.upper_ota_proxy or "")
    logger.info(f"will launch otaproxy at http://{host}:{port}, with {upper_proxy=}")
    if upper_proxy:
        logger.info(f"wait for {upper_proxy=} online...")
        ensure_otaproxy_start(str(upper_proxy))

    run_otaproxy(
        host=host,
        port=port,
        init_cache=init_cache,
        cache_dir=local_otaproxy_cfg.BASE_DIR,
        cache_db_f=local_otaproxy_cfg.DB_FILE,
        upper_proxy=upper_proxy,
        enable_cache=proxy_info.enable_local_ota_proxy_cache,
        enable_https=proxy_info.gateway_otaproxy,
        external_cache_mnt_point=external_cache_mnt_point,
    )


def otaproxy_control_thread(
    ecu_status_flags: MultipleECUStatusFlags,
    client_update_control_flags: ClientUpdateControlFlags,
) -> None:  # pragma: no cover
    atexit.register(_on_global_shutdown)

    _mp_ctx = mp.get_context("spawn")

    ota_cache_dir = Path(otaproxy_cfg.BASE_DIR)
    next_ota_cache_dir_checkpoint = 0
    otaproxy_min_alive_until = 0

    global _otaproxy_p
    while not _global_shutdown:
        time.sleep(OTAPROXY_CHECK_INTERVAL)
        _now = time.time()

        _otaproxy_running = _otaproxy_p and _otaproxy_p.is_alive()
        _otaproxy_should_run = ecu_status_flags.any_requires_network.is_set()
        _all_success = ecu_status_flags.all_success.is_set()
        _stop_server_event = client_update_control_flags.stop_server_event.is_set()

        if _stop_server_event:
            logger.info("shutting down otaproxy as client update requested ...")
            # kill the otaproxy process immediately
            _shutdown_otaproxy()
            return

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
                try:
                    shutil.rmtree(ota_cache_dir, ignore_errors=False)
                except PermissionError:
                    # in dynamic client, we can't remove /ota-cache because the root directory is RO.
                    # only cleanup the contents
                    with contextlib.suppress(Exception):
                        for item in ota_cache_dir.iterdir():
                            if item.is_file() or item.is_symlink():
                                item.unlink(missing_ok=True)
                            elif item.is_dir():
                                shutil.rmtree(item, ignore_errors=True)
                except Exception:
                    # ignore other exceptions
                    pass

        elif _otaproxy_should_run and not _otaproxy_running:
            # NOTE: always try to re-use cache. If the cache dir is empty, otaproxy
            #   will still init the cache even init_cache is False.
            _otaproxy_p = _mp_ctx.Process(
                target=partial(otaproxy_process, init_cache=False),
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
