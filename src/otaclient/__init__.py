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

from typing import TYPE_CHECKING

try:
    from _otaclient_version import __version__, version
except ImportError:
    # unknown version
    version = __version__ = "0.0.0"

__all__ = ["version", "__version__"]

if TYPE_CHECKING:
    import multiprocessing.synchronize as mp_sync

_global_shutdown_flag: mp_sync.Event | None = None
"""Maintained and set by main module."""


def global_shutdown() -> bool:
    return _global_shutdown_flag is not None and _global_shutdown_flag.is_set()
