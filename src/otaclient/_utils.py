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
"""Common shared utils, only used by otaclient package."""


from __future__ import annotations

import itertools
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Callable

from otaclient._types import OTAClientStatus
from otaclient_common._io import read_str_from_file, write_str_to_file_atomic
from otaclient_common.shm_status import MPSharedStatusReader, MPSharedStatusWriter
from otaclient_common.typing import StrOrPath

logger = logging.getLogger(__name__)


def wait_and_log(
    check_flag: Callable[[], bool],
    message: str = "",
    *,
    check_for: bool = True,
    check_interval: int = 2,
    log_interval: int = 30,
    log_func: Callable[[str], None] = logger.info,
) -> None:
    """Wait for <flag> until it is set while print a log every <log_interval>."""
    log_round = 0
    for seconds in itertools.count(step=check_interval):
        if check_flag() == check_for:
            return

        _new_log_round = seconds // log_interval
        if _new_log_round > log_round:
            log_func(f"wait for {message}: {seconds}s passed ...")
            log_round = _new_log_round
        time.sleep(check_interval)


def check_other_otaclient(pid_fpath: StrOrPath) -> None:
    """Check if there is another otaclient instance running, and then
    create a pid lock file for this otaclient instance."""
    pid_fpath = Path(pid_fpath)

    if pid := read_str_from_file(pid_fpath, _default=""):
        # running process will have a folder under /proc
        if Path(f"/proc/{pid}").is_dir():
            logger.error(f"another instance of ota-client({pid=}) is running, abort")
            sys.exit()

        logger.warning(f"dangling otaclient lock file({pid=}) detected, cleanup")
        Path(pid_fpath).unlink(missing_ok=True)

    write_str_to_file_atomic(pid_fpath, f"{os.getpid()}")


def create_otaclient_rundir(run_dir: StrOrPath = "/run/otaclient"):
    """Create the otaclient runtime working dir.

    TODO: make a helper class for managing otaclient runtime dir.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(exist_ok=True, parents=True)


def get_traceback(exc: Exception, *, splitter: str = "\n") -> str:
    """Format the <exc> traceback as string."""
    return splitter.join(traceback.format_exception(type(exc), exc, exc.__traceback__))


class SharedOTAClientStatusWriter(MPSharedStatusWriter[OTAClientStatus]):
    """Util for writing OTAClientStatus to shm."""


class SharedOTAClientStatusReader(MPSharedStatusReader[OTAClientStatus]):
    """Util for reading OTAClientStatus from shm."""


def gen_session_id(update_version: str) -> str:
    """Generate a unique session_id for the new OTA session.

    token schema:
        <update_version>-<unix_timestamp_in_sec_str>-<4bytes_hex>
    """
    _time_factor = str(int(time.time()))
    _random_factor = os.urandom(4).hex()

    return f"{update_version}-{_time_factor}-{_random_factor}"
