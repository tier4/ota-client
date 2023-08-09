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


class BaseConfig:
    LOGGING_FORMAT = "[%(asctime)s][%(levelname)s]: %(message)s"
    IMAGE_LAYOUT_VERSION = 1

    MANIFEST_JSON = "manifest.json"
    MANIFEST_SCHEMA_VERSION = 1
    OUTPUT_WORKDIR = "output"
    OUTPUT_DATA_DIR = "data"
    OUTPUT_META_DIR = "meta"
    IMAGE_UNARCHIVE_WORKDIR = "images"

    DEFAULT_OTA_METADATA_VERSION = 1
    OTA_METAFILES_LIST = [
        "certificate.pem",
        "metadata.jwt",
        "dirs.txt",
        "regulars.txt",
        "symlinks.txt",
        "persistents.txt",
    ]
    OTA_METAFILE_REGULAR = "regulars.txt"
    OTA_IMAGE_DATA_DIR = "data"
    OTA_IMAGE_DATA_ZST_DIR = "data.zst"
    OTA_IMAGE_COMPRESSION_ALG = "zst"

    EXTERNAL_CACHE_DEV_FSLABEL = "ota_cache_src"


cfg = BaseConfig()
