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


import asyncio
import os
import sys
from pathlib import Path

from otaclient import __version__  # type: ignore
from . import log_util
from .common import read_str_from_file, write_str_to_file_sync
from .configs import config as cfg, EXTRA_VERSION_FILE, OTACLIENT_LOCK_FILE
from .ota_client_service import launch_otaclient_grpc_server

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


def _check_other_otaclient():
    """Check if there is another otaclient instance running."""
    # create a lock file to prevent multiple ota-client instances start
    if pid := read_str_from_file(OTACLIENT_LOCK_FILE):
        # running process will have a folder under /proc
        if Path(f"/proc/{pid}").is_dir():
            logger.error(f"another instance of ota-client({pid=}) is running, abort")
            sys.exit()
        else:
            logger.warning(f"dangling otaclient lock file({pid=}) detected, cleanup")
            Path(OTACLIENT_LOCK_FILE).unlink(missing_ok=True)
    # write our pid to the lock file
    write_str_to_file_sync(OTACLIENT_LOCK_FILE, f"{os.getpid()}")


def main():
    logger.info("started")
    if Path(EXTRA_VERSION_FILE).is_file():
        logger.info(read_str_from_file(EXTRA_VERSION_FILE))
    logger.info(f"otaclient version: {__version__}")

    # start the otaclient grpc server
    _check_other_otaclient()
    asyncio.run(launch_otaclient_grpc_server())
