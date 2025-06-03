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

import logging
import multiprocessing.context as mp_ctx
import multiprocessing.shared_memory as mp_shm
import os
import sys
import time

from otaclient import __version__

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
    pass


def main() -> None:  # pragma: no cover
    from otaclient._logging import configure_logging
    from otaclient.client_package import OTAClientPackagePrepareter
    from otaclient.configs.cfg import cfg, ecu_info, proxy_info
    from otaclient_common import _env

    # configure logging before any code being executed
    configure_logging()

    logger.info("started")
    logger.info(f"otaclient version: {__version__}")
    logger.info(f"ecu_info.yaml: \n{ecu_info}")
    logger.info(f"proxy_info.yaml: \n{proxy_info}")
    logger.info(
        f"env.preparing_downloaded_dynamic_ota_client: {os.getenv(cfg.PREPARING_DOWNLOADED_DYNAMIC_OTA_CLIENT)}"
    )
    logger.info(
        f"env.running_downloaded_dynamic_ota_client: {os.getenv(cfg.RUNNING_DOWNLOADED_DYNAMIC_OTA_CLIENT)}"
    )

    if _env.is_dynamic_client_preparing() and not _env.is_dynamic_client_running():
        try:
            logger.info("in dynamic client preparation process ...")
            OTAClientPackagePrepareter().mount_client_package()

            running_env = os.environ.copy()
            del running_env[cfg.PREPARING_DOWNLOADED_DYNAMIC_OTA_CLIENT]
            running_env[cfg.RUNNING_DOWNLOADED_DYNAMIC_OTA_CLIENT] = "yes"
            os.execve(
                path=sys.executable,
                argv=[sys.executable, __file__],
                env=running_env,
            )
        except Exception as e:
            logger.exception(f"Failed during dynamic client preparation: {e}")
            return _on_shutdown(sys_exit=True)

    while True:
        time.sleep(HEALTH_CHECK_INTERVAL)
        # launch the dynamic client preparation process
        try:
            logger.info("preparing dynamic client package ...")
            preparing_env = os.environ.copy()
            preparing_env[cfg.PREPARING_DOWNLOADED_DYNAMIC_OTA_CLIENT] = "yes"
            # Execute with the modified environment
            os.execve(
                path=sys.executable,
                argv=[sys.executable, __file__],
                env=preparing_env,
            )
        except Exception as e:
            logger.exception(f"Failed during dynamic client preparation: {e}")
            return _on_shutdown(sys_exit=True)
