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
"""OTA image metadata, legacy version."""


from __future__ import annotations

import sys
from pathlib import Path

from otaclient_common import import_from_file

SUPORTED_COMPRESSION_TYPES = ("zst", "zstd")

# ------ dynamically import pb2 generated code ------ #

_PROTO_DIR = Path(__file__).parent
_PB2_FPATH = _PROTO_DIR / "ota_metafiles_pb2.py"
_PACKAGE_PREFIX = ".".join(__name__.split(".")[:-1])

_module_name, _module = import_from_file(_PB2_FPATH)
sys.modules[_module_name] = _module
sys.modules[f"{_PACKAGE_PREFIX}.{_module_name}"] = _module
