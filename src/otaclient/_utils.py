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
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Callable, Literal

from otaclient._types import OTAClientStatus
from otaclient.metrics import OTAMetricsSharedMemoryData
from otaclient_common._io import read_str_from_file, write_str_to_file_atomic
from otaclient_common._shm import MPSharedMemoryReader, MPSharedMemoryWriter
from otaclient_common._typing import StrOrPath

logger = logging.getLogger(__name__)


def wait_and_log(
    check_flag: Callable[[], bool],
    message: str = "",
    *,
    check_for: Literal[True] | Literal[False] = True,
    check_interval: int = 2,
    log_interval: int = 30,
    log_func: Callable[[str], None] = logger.info,
    timeout: int | None = None,
) -> bool:
    """Wait for <flag> until it is set while print a log every <log_interval>.

    Args:
        check_flag: Function that returns a boolean value to check.
        message: Message to include in the log output.
        check_for: The value to check against (True or False).
        check_interval: How often to check the flag in seconds.
        log_interval: How often to log a message in seconds.
        log_func: Function to use for logging.
        timeout: Maximum time to wait in seconds. None means wait indefinitely.

    Returns:
        bool: True if condition was met, False if timeout occurred.
    """
    log_round = 0
    for seconds in itertools.count(step=check_interval):
        if check_flag() == check_for:
            return True

        # Check if timeout has been reached
        if timeout is not None and seconds >= timeout:
            return False

        _new_log_round = seconds // log_interval
        if _new_log_round > log_round:
            log_func(f"wait for {message}: {seconds}s passed ...")
            log_round = _new_log_round
        time.sleep(check_interval)
    return False


def check_other_otaclient(pid_fpath: StrOrPath) -> None:  # pragma: no cover
    """Check if there is another otaclient instance running, and then
    create a pid lock file for this otaclient instance.

    NOTE that otaclient should not run inside a PID namespace.
    """
    pid_fpath = Path(pid_fpath)
    if pid := read_str_from_file(pid_fpath, _default=""):
        # running process will have a folder under /proc
        if Path(f"/proc/{pid}").is_dir():
            logger.error(f"another instance of ota-client({pid=}) is running, abort")
            sys.exit()
        logger.warning(f"dangling otaclient lock file({pid=}) detected, cleanup")
        pid_fpath.unlink(missing_ok=True)
    write_str_to_file_atomic(pid_fpath, f"{os.getpid()}")


def get_traceback(exc: Exception, *, splitter: str = "\n") -> str:  # pragma: no cover
    """Format the <exc> traceback as string."""
    return splitter.join(traceback.format_exception(type(exc), exc, exc.__traceback__))


class SharedOTAClientStatusWriter(MPSharedMemoryWriter[OTAClientStatus]):
    """Util for writing OTAClientStatus to shm."""


class SharedOTAClientStatusReader(MPSharedMemoryReader[OTAClientStatus]):
    """Util for reading OTAClientStatus from shm."""


class SharedOTAClientMetricsWriter(MPSharedMemoryWriter[OTAMetricsSharedMemoryData]):
    """Util for writing OTAMetricsSharedMemoryData to shm."""


class SharedOTAClientMetricsReader(MPSharedMemoryReader[OTAMetricsSharedMemoryData]):
    """Util for reading OTAMetricsSharedMemoryData from shm."""


SESSION_RANDOM_LEN = 4  # bytes, the corresponding hex string will be 8 chars

_illegal_chars_pattern = re.compile(r'[\.\/\0<>:"\\|?*\x00-\x1F]')


def gen_session_id(
    update_version: str, *, random_bytes_num: int = SESSION_RANDOM_LEN
) -> str:  # pragma: no cover
    """Generate a unique session_id for the new OTA session.

    token schema:
        <update_version>-<unix_timestamp_in_sec_str>-<4bytes_hex>
    """
    sanitized_version_str = _illegal_chars_pattern.sub("_", update_version)
    _time_factor = str(int(time.time()))
    _random_factor = os.urandom(random_bytes_num).hex()

    return f"{_time_factor}-{sanitized_version_str}-{_random_factor}"
