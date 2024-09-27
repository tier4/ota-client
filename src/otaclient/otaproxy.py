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
"""Control of the otaproxy server startup/shutdown."""


from __future__ import annotations

import logging
import multiprocessing.context as mp_ctx
import shutil
import sys
from pathlib import Path
from typing import Any, Optional, Type

from typing_extensions import Self

from ota_proxy import OTAProxyContextProto, subprocess_otaproxy_launcher
from ota_proxy import config as local_otaproxy_cfg
from otaclient import log_setting
from otaclient.app.configs import config as cfg
from otaclient.app.configs import proxy_info
from otaclient.boot_control._common import CMDHelperFuncs
from otaclient_common.common import ensure_otaproxy_start

logger = logging.getLogger(__name__)

_otaproxy_p: mp_ctx.SpawnProcess | None = None


class OTAProxyContext(OTAProxyContextProto):
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

    @property
    def extra_kwargs(self) -> dict[str, Any]:
        """Inject kwargs to otaproxy startup entry.

        Currently only inject <external_cache> if external cache storage is used.
        """
        _res = {}
        if self.external_cache_enabled and self._external_cache_activated:
            _res[self.EXTERNAL_CACHE_KEY] = self._external_cache_data_dir
        else:
            _res.pop(self.EXTERNAL_CACHE_KEY, None)
        return _res

    def _subprocess_init(self):
        """Initializing the subprocess before launching it."""
        # configure logging for otaproxy subprocess
        # NOTE: on otaproxy subprocess, we first set log level of the root logger
        #       to CRITICAL to filter out third_party libs' logging(requests, urllib3, etc.),
        #       and then set the ota_proxy logger to DEFAULT_LOG_LEVEL
        log_setting.configure_logging()
        otaproxy_logger = logging.getLogger("ota_proxy")
        otaproxy_logger.setLevel(cfg.DEFAULT_LOG_LEVEL)

        # wait for upper otaproxy if any
        if self.upper_proxy:
            otaproxy_logger.info(f"wait for {self.upper_proxy=} online...")
            ensure_otaproxy_start(str(self.upper_proxy))

    def _mount_external_cache_storage(self):
        # detect cache_dev on every startup
        _cache_dev = CMDHelperFuncs.get_dev_by_token(
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
        CMDHelperFuncs.umount(_cache_dev, raise_exception=False)
        _mp.mkdir(parents=True, exist_ok=True)

        # try to mount cache_dev ro
        try:
            CMDHelperFuncs.mount_ro(
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
            CMDHelperFuncs.umount(self._external_cache_dev)
        except Exception as e:
            logger.warning(
                f"failed to unmount external cache_dev {self._external_cache_dev}: {e!r}"
            )
        finally:
            self.started = self._external_cache_activated = False

    def __enter__(self) -> Self:
        try:
            self._subprocess_init()
            self._mount_external_cache_storage()
            return self
        except Exception as e:
            # if subprocess init failed, directly let the process exit
            logger.error(f"otaproxy subprocess init failed, exit: {e!r}")
            sys.exit(1)

    def __exit__(
        self,
        __exc_type: Optional[Type[BaseException]],
        __exc_value: Optional[BaseException],
        __traceback,
    ) -> Optional[bool]:
        if __exc_type:
            _exc = __exc_value if __exc_value else __exc_type()
            logger.warning(f"exception during otaproxy shutdown: {_exc!r}")
        # otaproxy post-shutdown cleanup:
        #   1. umount external cache storage
        self._umount_external_cache_storage()


def cleanup_cache_dir():
    """Cleanup the OTA cache dir.

    NOTE: this method should only be called when all ECUs in the cluster
            are in SUCCESS ota_status(overall_ecu_status.all_success==True).
    """
    if (cache_dir := Path(local_otaproxy_cfg.BASE_DIR)).is_dir():
        logger.info("cleanup ota_cache on success")
        shutil.rmtree(cache_dir, ignore_errors=True)


def start_otaproxy_server(subprocess_ctx: OTAProxyContext, *, init_cache: bool) -> None:
    if _otaproxy_p:
        logger.warning("otaproxy is already running, abort")
        return

    _subprocess_entry = subprocess_otaproxy_launcher(subprocess_ctx)
    host, port = (
        str(proxy_info.local_ota_proxy_listen_addr),
        proxy_info.local_ota_proxy_listen_port,
    )
    upper_proxy = str(proxy_info.upper_ota_proxy or "")

    global _otaproxy_p
    _otaproxy_p = _subprocess_entry(
        host=host,
        port=port,
        init_cache=init_cache,
        cache_dir=local_otaproxy_cfg.BASE_DIR,
        cache_db_f=local_otaproxy_cfg.DB_FILE,
        upper_proxy=upper_proxy,
        enable_cache=proxy_info.enable_local_ota_proxy_cache,
        enable_https=proxy_info.gateway_otaproxy,
    )
    _otaproxy_p.start()
    logger.info(f"otaproxy started at http://{host}:{port}, with {upper_proxy=}")


def shutdown_otaproxy_server() -> None:
    global _otaproxy_p
    if _otaproxy_p and _otaproxy_p.is_alive():
        logger.info("shuting down otaproxy server process...")
        _otaproxy_p.terminate()
        _otaproxy_p.join()
    _otaproxy_p = None
    logger.info("otaproxy closed")
