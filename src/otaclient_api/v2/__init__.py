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
"""OTAClient API, version 2."""


from __future__ import annotations

import sys
from pathlib import Path

from otaclient_common import import_from_file

# ------ dynamically import pb2 generated code ------ #

_PROTO_DIR = Path(__file__).parent
# NOTE: order matters here! pb2_grpc depends on pb2
_FILES_TO_LOAD = [
    _PROTO_DIR / "otaclient_v2_pb2.py",
    _PROTO_DIR / "otaclient_v2_pb2_grpc.py",
]
PACKAGE_PREFIX = ".".join(__name__.split(".")[:-1])


def _import_pb2_proto(*module_fpaths: Path):
    """Import the protobuf modules to path under this folder.

    NOTE: compiled protobuf files under proto folder will be
    imported as modules to the global namespace.
    """
    for _fpath in module_fpaths:
        _module_name, _module = import_from_file(_fpath)
        sys.modules[f"{PACKAGE_PREFIX}.{_module_name}"] = _module
        sys.modules[_module_name] = _module


_import_pb2_proto(*_FILES_TO_LOAD)
del _import_pb2_proto
