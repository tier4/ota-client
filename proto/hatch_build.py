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
"""Dynamically generate protobuf python code."""


from __future__ import annotations

import os
import string
import subprocess
import sys
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

INIT_TEMPLATE = r"""\
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import sys

from . import ${pb2_module_name} as pb2

__version__ = version = "${api_version}"
__version_tuple__ = version_tuple = tuple(version.split("."))

__all__ = ["version", "__version__", "version_tuple", "__version_tuple__"]

# also place the pb2 module directly under the global namespace, this is required by
#   pb2_grpc module as pb2_grpc module imports pb2 moduel without any prefix.
sys.modules["${pb2_module_name}"] = pb2
"""


def _protoc_compile(
    proto_file: str,
    *,
    extra_imports: list[str] | None,
    output_dir: str | Path,
    work_dir: str | Path,
):
    # fmt: off
    cmd = [
        sys.executable, "-m", "grpc_tools.protoc",
        f"--python_out={output_dir}",
        f"--pyi_out={output_dir}",
        f"--grpc_python_out={output_dir}", 
    ]
    # fmt: on
    if isinstance(extra_imports, list):
        for _import in extra_imports:
            cmd.append(f"-I{_import}")

    cmd.append(proto_file)
    print(f"call protoc with: {' '.join(cmd)}")
    subprocess.check_call(cmd, cwd=work_dir)


def _get_built_pb2_module_name(proto_file: str) -> str:
    """
    built module name will be the proto_file name + "_pb2".

    For example, from "abc.proto" we will get module named "abc_pb2".
    """
    proto_fname = os.path.basename(proto_file)
    _split = os.path.splitext(proto_fname)
    return f"{_split[0]}_pb2"


def _generate__init__(api_version: str, proto_file: str, *, output_dir: str | Path):
    pb2_module_name = _get_built_pb2_module_name(proto_file)
    template = string.Template(INIT_TEMPLATE)
    init_file = Path(output_dir) / "__init__.py"
    init_file.write_text(
        template.substitute(
            api_version=api_version,
            pb2_module_name=pb2_module_name,
        )
    )


class CustomBuildHook(BuildHookInterface):
    """
    Configs:
        proto_file: file to generate python code against.
        extra_imports: a list of paths for proto imports.
        output_dir: where to put the built python code to.
        api_version: the API version of otaclient service API.
    """

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        config = self.config

        # ------ parse config ------ #
        output_dir = config["output_dir"]
        proto_file = config["proto_file"]
        extra_imports = config.get("extra_imports", [])
        api_version = config.get("api_version", "0.0.0")

        # ------ validate config ------ #
        if not isinstance(extra_imports, list):
            raise ValueError(
                f"expect extra_imports to be a list, get {type(extra_imports)}"
            )

        if not os.path.exists(proto_file):
            raise FileNotFoundError(f"{proto_file=} not found, abort")

        # let the folder where proto file exists become the work_dir
        work_dir = os.path.dirname(proto_file)
        if not work_dir:
            work_dir = "."
        # always place the folder where proto_file exists
        extra_imports.append(work_dir)

        # ------ compile proto file ------ #
        _protoc_compile(
            proto_file,
            extra_imports=extra_imports,
            output_dir=output_dir,
            work_dir=work_dir,
        )
        _generate__init__(
            api_version=api_version, proto_file=proto_file, output_dir=output_dir
        )
