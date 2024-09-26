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
import os
import sys
from pathlib import Path

from otaclient import __version__
from otaclient.api_v2.server import app_server_main
from otaclient.app.configs import config as cfg
from otaclient.app.configs import ecu_info
from otaclient.log_setting import configure_logging
from otaclient.ota_app import ota_app_main
from otaclient_common.common import read_str_from_file, write_str_to_file_sync

# configure logging before any code being executed
configure_logging()
logger = logging.getLogger("otaclient")

_ota_server_p: mp_ctx.SpawnProcess | None = None
_ota_core_p: mp_ctx.SpawnProcess | None = None


def _global_shutdown():
    if _ota_server_p:
        _ota_server_p.join()
    if _ota_core_p:
        _ota_core_p.join()


atexit.register(_global_shutdown)


def _check_other_otaclient():
    """Check if there is another otaclient instance running."""
    # create a lock file to prevent multiple ota-client instances start
    if pid := read_str_from_file(cfg.OTACLIENT_PID_FILE):
        # running process will have a folder under /proc
        if Path(f"/proc/{pid}").is_dir():
            logger.error(f"another instance of ota-client({pid=}) is running, abort")
            sys.exit()
        else:
            logger.warning(f"dangling otaclient lock file({pid=}) detected, cleanup")
            Path(cfg.OTACLIENT_PID_FILE).unlink(missing_ok=True)
    # create run dir
    _run_dir = Path(cfg.RUN_DIR)
    _run_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(_run_dir, 0o550)
    # write our pid to the lock file
    write_str_to_file_sync(cfg.OTACLIENT_PID_FILE, f"{os.getpid()}")


def main() -> None:
    logger.info("started")
    logger.info(f"otaclient version: {__version__}")
    logger.info(f"ecu_info.yaml: \n{ecu_info}")

    # start the otaclient grpc server
    _check_other_otaclient()

    ctx = mp.get_context("spawn")

    permit_reboot_flag = ctx.Event()
    ipc_status_report_queue = ctx.Queue()
    ipc_ota_op_queue = ctx.Queue()

    global _ota_core_p, _ota_server_p
    _ota_core_p = ctx.Process(
        target=ota_app_main,
        kwargs={
            "status_report_queue": ipc_status_report_queue,
            "opeartion_queue": ipc_ota_op_queue,
            "control_flag": permit_reboot_flag,
        },
        daemon=True,
    )
    _ota_server_p = ctx.Process(
        target=app_server_main,
        kwargs={
            "status_report_queue": ipc_status_report_queue,
            "opeartion_queue": ipc_ota_op_queue,
            "control_flag": permit_reboot_flag,
        },
        daemon=True,
    )

    _ota_core_p.start()
    _ota_server_p.start()

    _ota_core_p.join()
    _ota_server_p.join()


if __name__ == "__main__":
    main()
