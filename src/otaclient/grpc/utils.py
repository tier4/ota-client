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

import logging
import os
import sys
from pathlib import Path

from otaclient_common._io import read_str_from_file, write_str_to_file_atomic
from otaclient_common.typing import StrOrPath

logger = logging.getLogger(__name__)


def check_other_otaclient(pid_fpath: StrOrPath):
    """Check if there is another otaclient instance running."""
    pid_fpath = Path(pid_fpath)

    # create a lock file to prevent multiple ota-client instances start
    if pid := read_str_from_file(pid_fpath, _default=""):
        # running process will have a folder under /proc
        if Path(f"/proc/{pid}").is_dir():
            logger.error(f"another instance of ota-client({pid=}) is running, abort")
            sys.exit()
        else:
            logger.warning(f"dangling otaclient lock file({pid=}) detected, cleanup")
            Path(pid_fpath).unlink(missing_ok=True)

    # write our pid to the lock file
    write_str_to_file_atomic(pid_fpath, f"{os.getpid()}")


def create_otaclient_rundir(run_dir: StrOrPath = "/run/otaclient"):
    """Create the otaclient runtime working dir."""
    run_dir = Path(run_dir)
    run_dir.mkdir(exist_ok=True, parents=True)
