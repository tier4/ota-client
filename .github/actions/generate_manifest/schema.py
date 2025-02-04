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
"""Schema definition for manifest.json."""

from datetime import datetime
from typing import List, Literal

from pydantic import BaseModel


class ReleasePackage(BaseModel):
    filename: str
    version: str
    type: Literal["squashfs", "patch"]
    architecture: Literal["x86_64", "arm64"]
    size: int
    checksum: str


class Manifest(BaseModel):
    schema_version: str = "1"
    date: datetime
    packages: List[ReleasePackage]
