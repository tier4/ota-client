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


"""Packed compiled protobuf files for otaclient."""
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Tuple

_PROTO_DIR = Path(__file__).parent
# NOTE: order matters here! v2_pb2_grpc depends on v2_pb2
_FILES_TO_LOAD = [
    _PROTO_DIR / _fname
    for _fname in [
        "otaclient_v2_pb2.py",
        "otaclient_v2_pb2_grpc.py",
    ]
]


def _import_from_file(path: Path) -> Tuple[str, ModuleType]:
    if not path.is_file():
        raise ValueError(f"{path} is not a valid module file")
    try:
        _module_name = path.stem
        _spec = importlib.util.spec_from_file_location(_module_name, path)
        _module = importlib.util.module_from_spec(_spec)  # type: ignore
        _spec.loader.exec_module(_module)  # type: ignore
        return _module_name, _module
    except Exception:
        raise ImportError(f"failed to import module from {path=}.")


def _import_proto(*module_fpaths: Path):
    """Import the protobuf modules to path under this folder.

    NOTE: compiled protobuf files under proto folder will be
    imported as modules to the global namespace.
    """
    for _fpath in module_fpaths:
        _module_name, _module = _import_from_file(_fpath)
        # add the module to the global module namespace
        sys.modules[_module_name] = _module


_import_proto(*_FILES_TO_LOAD)
del _import_proto, _import_from_file

import otaclient_v2_pb2 as v2
import otaclient_v2_pb2_grpc as v2_grpc
from . import wrapper

__all__ = ["v2", "v2_grpc", "wrapper"]
