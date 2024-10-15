# This script requires python 3.12 to run.
from __future__ import annotations

import sys
import tomllib
from typing import Any


def gen_requirements_txt(pyproject_cfg: dict[str, Any]) -> str:
    try:
        _res: list[str] = pyproject_cfg["project"]["dependencies"]
        return "\n".join(_res)
    except KeyError:
        return ""


if __name__ == "__main__":
    script_name, *args = sys.argv
    if len(args) < 2:
        print(f"Usage: {script_name} <pyproject.toml> <requirements.txt>")
        sys.exit(1)

    pyproject_toml, requirements_txt, *_ = args
    with open(pyproject_toml, "rb") as src, open(requirements_txt, "w") as dst:
        _parsed = tomllib.load(src)
        dst.write(gen_requirements_txt(_parsed))
