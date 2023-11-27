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
import asyncio
import os
import os.path
import sys
from pathlib import Path

# NOTE: preload protobuf/grpc related modules before any other component
#       modules being imported.
from .proto import wrapper, v2, v2_grpc, ota_metafiles  # noqa: F401

from otaclient import __version__  # type: ignore
from otaclient._utils import if_run_as_container
from .common import read_str_from_file, write_str_to_file_sync
from .configs import config as cfg, logging_config, EXTRA_VERSION_FILE
from .log_setting import configure_logging, get_ecu_id, get_logger
from .ota_client_service import launch_otaclient_grpc_server

# configure logging before any code being executed
configure_logging(logging_config.LOGGING_LEVEL, http_logging_url=get_ecu_id())
logger = get_logger(__name__)


def _check_other_otaclient():
    """Check if there is another otaclient instance running."""
    # create a lock file to prevent multiple ota-client instances start
    if pid := read_str_from_file(cfg.OTACLIENT_PID_FPATH):
        # running process will have a folder under /proc
        if Path(f"/proc/{pid}").is_dir():
            logger.error(f"another instance of ota-client({pid=}) is running, abort")
            sys.exit()
        else:
            logger.warning(f"dangling otaclient lock file({pid=}) detected, cleanup")
            Path(cfg.OTACLIENT_PID_FPATH).unlink(missing_ok=True)
    # create run dir
    _run_dir = Path(cfg.RUN_DPATH)
    _run_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(_run_dir, 0o550)
    # write our pid to the lock file
    write_str_to_file_sync(cfg.OTACLIENT_PID_FPATH, f"{os.getpid()}")


def _check_active_rootfs():
    """Checking the ACTIVE_ROOTFS config value when in container mode."""
    active_rootfs_mp = cfg.ACTIVE_ROOTFS
    if active_rootfs_mp == cfg.DEFAULT_ACTIVE_ROOTFS:
        return

    assert os.path.isdir(
        active_rootfs_mp
    ), f"ACTIVE_ROOTFS must be a dir, get {active_rootfs_mp}"
    assert os.path.isabs(
        active_rootfs_mp
    ), f"ACTIVE_ROOTFS must be absolute, get: {active_rootfs_mp}"


def main():
    logger.info("started")
    if Path(EXTRA_VERSION_FILE).is_file():
        logger.info(read_str_from_file(EXTRA_VERSION_FILE))
    logger.info(f"otaclient version: {__version__}")

    # issue a warning if otaclient detects itself is running as container,
    # but config.IS_CONTAINER is not True(ACTIVE_ROOTFS is not configured).
    # TODO: do more things over this unexpected condition?
    if if_run_as_container() and not cfg.IS_CONTAINER:
        logger.warning(
            "otaclient seems to run as container, but host rootfs is not mounted into the container "
            "and/or ACTIVE_ROOTFS not specified"
        )

    # do pre-start checking
    _check_active_rootfs()
    _check_other_otaclient()

    asyncio.run(launch_otaclient_grpc_server())
