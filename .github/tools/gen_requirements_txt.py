# This script requires python 3.12 to run.
from __future__ import annotations

import sys
import tomllib
from typing import Any


def gen_requirements_txt(pyproject_cfg: dict[str, Any]) -> str:
    _res: list[str] = [
        "# Automatically generated from pyproject.toml by gen_requirements_txt.py script.",
        "# DO NOT EDIT! Only for reference use.",
    ]
    try:
        _res.extend(pyproject_cfg["project"]["dependencies"])
    except KeyError:
        print("WARNING: no deps are defined in pyproject.toml")
    _res.append("")
    return "\n".join(_res)


if __name__ == "__main__":
    script_name, *args = sys.argv
    if len(args) < 2:
        print(f"Usage: {script_name} <pyproject.toml> <requirements.txt>")
        sys.exit(1)

    pyproject_toml, requirements_txt, *_ = args
    with open(pyproject_toml, "rb") as src, open(requirements_txt, "w") as dst:
        _parsed = tomllib.load(src)
        dst.write(gen_requirements_txt(_parsed))
