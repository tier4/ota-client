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

import asyncio
import atexit
import logging
import multiprocessing as mp
import multiprocessing.context as mp_ctx
import multiprocessing.synchronize as mp_sync
import shutil
import time
from functools import partial
from pathlib import Path

from ota_proxy import config as local_otaproxy_cfg
from ota_proxy import run_otaproxy
from otaclient.configs.cfg import cfg, proxy_info
from otaclient_common import cmdhelper
from otaclient_common.common import ensure_otaproxy_start

logger = logging.getLogger(__name__)

_otaproxy_p: mp_ctx.SpawnProcess | None = None


def shutdown_otaproxy_server() -> None:
    global _otaproxy_p
    if _otaproxy_p:
        _otaproxy_p.terminate()
        _otaproxy_p.join()
        _otaproxy_p = None


OTAPROXY_CHECK_INTERVAL = 3
OTAPROXY_MIN_STARTUP_TIME = 60
"""Keep otaproxy running at least 60 seconds after startup."""
OTA_CACHE_DIR_CHECK_INTERVAL = 60
SHUTDOWN_AFTER_CORE_EXIT = 16
SHUTDOWN_AFTER_API_SERVER_EXIT = 3


class OTAProxyContext:
    EXTERNAL_CACHE_KEY = "external_cache"

    def __init__(
        self,
        *,
        external_cache_enabled: bool = True,
        external_cache_dev_fslable: str = cfg.EXTERNAL_CACHE_DEV_FSLABEL,
        external_cache_dev_mp: str = cfg.EXTERNAL_CACHE_DEV_MOUNTPOINT,
        external_cache_path: str = cfg.EXTERNAL_CACHE_SRC_PATH,
    ) -> None:
        self.upper_proxy = proxy_info.upper_ota_proxy
        self.external_cache_enabled = external_cache_enabled

        self._external_cache_activated = False
        self._external_cache_dev_fslabel = external_cache_dev_fslable
        self._external_cache_dev = None  # type: ignore[assignment]
        self._external_cache_dev_mp = external_cache_dev_mp
        self._external_cache_data_dir = external_cache_path

    def _mount_external_cache_storage(self) -> None:
        # detect cache_dev on every startup
        _cache_dev = cmdhelper.get_dev_by_token(
            "LABEL",
            self._external_cache_dev_fslabel,
            raise_exception=False,
        )
        if not _cache_dev:
            return

        if len(_cache_dev) > 1:
            logger.warning(
                f"multiple external cache storage device found, use the first one: {_cache_dev[0]}"
            )
        _cache_dev = _cache_dev[0]

        logger.info(f"external cache dev detected at {_cache_dev}")
        self._external_cache_dev = _cache_dev

        # try to unmount the mount_point and cache_dev unconditionally
        _mp = Path(self._external_cache_dev_mp)
        cmdhelper.umount(_cache_dev, raise_exception=False)
        _mp.mkdir(parents=True, exist_ok=True)

        try:
            cmdhelper.mount_ro(
                target=_cache_dev, mount_point=self._external_cache_dev_mp
            )
            self._external_cache_activated = True
        except Exception as e:
            logger.warning(
                f"failed to mount external cache dev({_cache_dev}) to {self._external_cache_dev_mp=}: {e!r}"
            )

    def _umount_external_cache_storage(self):
        if not self._external_cache_activated or not self._external_cache_dev:
            return
        try:
            cmdhelper.umount(self._external_cache_dev)
        except Exception as e:
            logger.warning(
                f"failed to unmount external cache_dev {self._external_cache_dev}: {e!r}"
            )
        finally:
            self.started = self._external_cache_activated = False

    def __enter__(self) -> str | None:
        try:
            self._mount_external_cache_storage()
            if self._external_cache_activated:
                return self._external_cache_data_dir
        except Exception as e:
            logger.warning(f"failed to enable external cache source: {e!r}")

    def __exit__(
        self,
        __exc_type: type[BaseException] | None,
        __exc_value: BaseException | None,
        __traceback,
    ):
        if __exc_type:
            _exc = __exc_value if __exc_value else __exc_type()
            logger.warning(f"exception during otaproxy shutdown: {_exc!r}")
            return True  # suppress exception

        try:
            # otaproxy post-shutdown cleanup:
            #   1. umount external cache storage
            self._umount_external_cache_storage()
        except Exception as e:
            logger.warning(f"failed to umount external cache source: {e!r}")


def otaproxy_process(*, init_cache: bool, enable_external_cache: bool) -> None:
    from otaclient._logging import configure_logging

    configure_logging()
    logger.info("otaproxy process started")

    host, port = (
        str(proxy_info.local_ota_proxy_listen_addr),
        proxy_info.local_ota_proxy_listen_port,
    )

    upper_proxy = str(proxy_info.upper_ota_proxy or "")
    logger.info(f"will launch otaproxy at http://{host}:{port}, with {upper_proxy=}")
    if upper_proxy:
        logger.info(f"wait for {upper_proxy=} online...")
        ensure_otaproxy_start(str(upper_proxy))

    with OTAProxyContext(external_cache_enabled=enable_external_cache) as _cache_dir:
        asyncio.run(
            run_otaproxy(
                host=host,
                port=port,
                init_cache=init_cache,
                cache_dir=local_otaproxy_cfg.BASE_DIR,
                cache_db_f=local_otaproxy_cfg.DB_FILE,
                upper_proxy=upper_proxy,
                enable_cache=proxy_info.enable_local_ota_proxy_cache,
                enable_https=proxy_info.gateway_otaproxy,
                external_cache=_cache_dir,
            )
        )


def otaproxy_control_thread(
    *,
    any_requires_network: mp_sync.Event,
    all_ecus_succeeded: mp_sync.Event,
) -> None:  # pragma: no cover
    atexit.register(shutdown_otaproxy_server)

    _mp_ctx = mp.get_context("spawn")

    ota_cache_dir = Path(local_otaproxy_cfg.BASE_DIR)
    next_ota_cache_dir_checkpoint = 0

    global _otaproxy_p
    while True:
        _now = time.time()
        time.sleep(OTAPROXY_CHECK_INTERVAL)

        _otaproxy_running = _otaproxy_p and _otaproxy_p.is_alive()
        _otaproxy_should_run = any_requires_network.is_set()

        if not _otaproxy_should_run and not _otaproxy_running:
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
            continue

        if _otaproxy_should_run and not _otaproxy_running:
            # NOTE: always try to re-use cache. If the cache dir is empty, otaproxy
            #   will still init the cache even init_cache is False.
            _otaproxy_p = _mp_ctx.Process(
                target=partial(
                    otaproxy_process,
                    init_cache=False,
                    enable_external_cache=True,
                ),
                name="otaproxy",
            )
            _otaproxy_p.start()
            next_ota_cache_dir_checkpoint = _now + OTAPROXY_MIN_STARTUP_TIME
            time.sleep(OTAPROXY_MIN_STARTUP_TIME)  # prevent pre-mature shutdown
            continue

        if not _otaproxy_should_run and _otaproxy_running:
            logger.info("shutting down otaproxy as not needed now ...")
            shutdown_otaproxy_server()
