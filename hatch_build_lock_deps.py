# A custom metadata hook implementation that overwrites dependencies with locked version
#   from uv.lock file.
#
# reference https://github.com/pga2rn/py_deps_locked_build_demo

from __future__ import annotations

import os
import subprocess
from pprint import pformat

from hatchling.metadata.plugin.interface import MetadataHookInterface

# NOTE: ! VERY IMPORTANT !
#       We should ONLY enable dependencies overwrite when needed, otherwise
#           `uv sync` or install project as editable will not work due to conflicts!
HINT_ENV_NAME = "ENABLE_DEPS_LOCKED_BUILD"
HINT_ENV_VALUE = "true"

# fmt: off
UV_EXPORT_FREEZED_CMD = (
    "uv", "export", "--frozen", "--color=never",
    "--no-dev", "--no-editable", "--no-hashes", "--no-emit-project",
)
# fmt: on


def _uv_export_locked_requirements() -> list[str]:
    print(
        f"metahook: generate locked deps list with: {' '.join(UV_EXPORT_FREEZED_CMD)}"
    )
    _uv_export_res = subprocess.run(UV_EXPORT_FREEZED_CMD, capture_output=True)
    _res: list[str] = []
    for _dep_line in _uv_export_res.stdout.decode().splitlines():
        _dep_line = _dep_line.strip()
        if _dep_line.startswith("#"):
            continue
        _res.append(_dep_line)
    return _res


class CustomMetadataHook(MetadataHookInterface):
    def update(self, metadata) -> None:
        if os.environ.get(HINT_ENV_NAME) == HINT_ENV_VALUE:
            print(f"metahook: {HINT_ENV_NAME} is configured.")
            print("metahook: will overwrite dependencies with locked version.")
            _locked_deps = _uv_export_locked_requirements()
            print(f"metahook: locked deps: {pformat(_locked_deps)}")
            metadata["dependencies"] = _locked_deps
