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
"""Shared utils among package."""


from os import PathLike
from pathlib import Path
from typing import Union
from typing_extensions import TypeAlias

from otaclient.app.common import subprocess_call

StrPath: TypeAlias = Union[str, PathLike]


class InputImageProcessError(Exception):
    ...


class ExportError(Exception):
    ...


PROC_MOUNTS = "/proc/mounts"


def check_if_mounted(dev: StrPath) -> bool:
    """Perform a fast check to see if <dev> is mounted."""
    for line in Path(PROC_MOUNTS).read_text().splitlines():
        if line.find(str(dev)) != -1:
            return True
    return False


def umount(dev: StrPath):
    _cmd = f"umount {dev}"
    subprocess_call(_cmd, raise_exception=True)
