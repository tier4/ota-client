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
"""Offline OTA image metadata

Version 1 schema:
{
  "version": 1,
  # the original size of this offline OTA image bundle
  "image_size": <size>,
  # the number of files in this image bundle 
  "unique_files_num": <size>,
  # a list of ECUs that have images prepared in this offline OTA image
  "ecu_ids": [...],
  # metadata for included images
  "image_metadata": {
    <ecu_id>: {
      "image_name": <image_name>,
      "image_version": <image_version_str>,
      # the directory under this image bundle to the image metadata
      "meta_dir": "meta/<ecu_id>",
      # the version of metadata.jwt
      "metadata_jwt_version": 1,
    },
    ...
  }
  # the directory that contains all files of this image bundle
  "data_dir": "data",
  "compression_alg": "zst",
}
"""


import json
from dataclasses import dataclass, field, asdict
from typing import List

from .configs import cfg


@dataclass
class ImageMetadata:
    metadata_jwt_version: int = cfg.DEFAULT_OTA_METADATA_VERSION
    image_name: str = ""
    image_version: str = ""
    meta_dir: str = ""


@dataclass
class Manifest:
    version: int = cfg.MANIFEST_VERSION
    image_size: int = 0
    total_files_num: int = 0
    data_dir: str = cfg.OUTPUT_DATA_DIR
    compression_alg: str = cfg.OTA_IMAGE_COMPRESSION_ALG
    ecu_ids: List[str] = field(default_factory=list)
    image_metadata: List[ImageMetadata] = field(default_factory=list)

    def export_to_json(self) -> str:
        return json.dumps(asdict(self))
