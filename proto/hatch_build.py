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
import shutil
import string
import subprocess
import sys
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

INIT_TEMPLATE = """\
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

__version__ = version = "${api_version}"
__version_tuple__ = version_tuple = tuple(version.split("."))

__all__ = ["version", "__version__", "version_tuple", "__version_tuple__"]
"""
OUTPUT_BASE = "src"


def _protoc_compile(
    proto_file: str,
    output_base: str,
    output_package: str,
    *,
    extra_imports: list[str] | None,
    work_dir: str | Path,
):
    # copy the proto_file to the output_dir to retain the proper package import layout
    output_package_dir = Path(output_base) / output_package
    shutil.copy(proto_file, output_package_dir)
    _proto_file = output_package_dir / os.path.basename(proto_file)

    # fmt: off
    cmd = [
        sys.executable, "-m", "grpc_tools.protoc",
        f"--python_out={output_base}",
        f"--pyi_out={output_base}",
        f"--grpc_python_out={output_base}",
    ]
    # fmt: on
    if isinstance(extra_imports, list):
        for _import in extra_imports:
            cmd.append(f"-I{_import}")
    # also place the output_base as import path
    cmd.append(f"-I{output_base}")

    cmd.append(str(_proto_file))
    print(f"call protoc with: {' '.join(cmd)}")
    subprocess.check_call(cmd, cwd=work_dir)


def _generate__init__(api_version: str, *, output_dir: str | Path):
    template = string.Template(INIT_TEMPLATE)
    init_file = Path(output_dir) / "__init__.py"
    init_file.write_text(template.substitute(api_version=api_version))


class CustomBuildHook(BuildHookInterface):
    """
    Configs:
        api_version: the API version of otaclient service API.
    """

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        for config in self.config["proto_builds"]:
            # ------ parse config ------ #
            proto_file = config["proto_file"]
            print(f"build protoc python code for {proto_file} ...")

            output_package = config["output_package"]
            extra_imports = config.get("extra_imports", [])
            api_version = config.get("api_version", "0.0.0")

            # ------ load consts ------ #
            output_base = OUTPUT_BASE

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

            # ------ compile proto file ------ #
            _protoc_compile(
                proto_file,
                extra_imports=extra_imports,
                output_base=output_base,
                output_package=output_package,
                work_dir=work_dir,
            )
            _generate__init__(
                api_version=api_version, output_dir=Path(output_base) / output_package
            )
