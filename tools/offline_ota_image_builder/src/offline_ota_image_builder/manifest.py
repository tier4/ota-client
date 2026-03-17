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
"""External cache source image manifest definition

A JSON file manifest.json that contains the basic information of built
external cache source image will be placed at the image rootfs.

Check README.md for the spec.
"""


import json
from dataclasses import asdict, dataclass, field
from typing import List

from .configs import cfg


@dataclass
class ImageMetadata:
    ecu_id: str = ""
    image_version: str = ""
    ota_metadata_version: int = cfg.DEFAULT_OTA_METADATA_VERSION


@dataclass
class Manifest:
    schema_version: int = cfg.MANIFEST_SCHEMA_VERSION
    image_layout_version: int = cfg.IMAGE_LAYOUT_VERSION
    build_timestamp: int = 0
    data_size: int = 0
    data_files_num: int = 0
    meta_size: int = 0
    image_meta: List[ImageMetadata] = field(default_factory=list)

    def export_to_json(self) -> str:
        return json.dumps(asdict(self))
