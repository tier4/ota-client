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


import os
from setuptools import setup, find_packages
import subprocess


def get_version():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(current_dir, "..", "VERSION"), mode="r") as f:
        return f.read().strip()


def get_git_hash():
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], stdout=subprocess.PIPE, text=True
    )
    return result.stdout.strip()


pkg_name = "otaclient_pb2"

version = get_version()
git_hash = get_git_hash()
whl_version = f"{version}.{git_hash}"

setup(
    name=pkg_name,
    version=whl_version,
    packages=find_packages(),
    description="ota client protobuf package",
    url="https://github.com/tier4/ota-client",
    author="Tier4 FMS Development Team",
)
