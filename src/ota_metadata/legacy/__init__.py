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

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_PROTO_DIR = Path(__file__).parent
_PB2_FPATH = _PROTO_DIR / "ota_metafiles_pb2.py"
_PACKAGE_PREFIX = "ota_metadata.legacy"


def _import_from_file(path: Path) -> tuple[str, ModuleType]:
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


_module_name, _module = _import_from_file(_PB2_FPATH)  # noqa: F821
sys.modules[_module_name] = _module
sys.modules[f"{_PACKAGE_PREFIX}.{_module_name}"] = _module

# For OTA image format legacy, we support zst,zstd compression.
SUPORTED_COMPRESSION_TYPES = ("zst", "zstd")
