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
"""External cache source builder helper.

Please refere to https://github.com/tier4/ota-metadata for
OTA image layout specification.
This tool is implemented based on ota-metadata@a711013.

Example OTA image layout:
.
├── data
│   ├── <...files with full paths...>
│   └── ...
├── data.zst
│   ├── <... compressed OTA file ...>
│   └── <... naming: <file_sha256>.zst ...>
├── certificate.pem
├── dirs.txt
├── metadata.jwt
├── persistents.txt
├── regulars.txt
└── symlinks.txt


The generated external cache source image will have the following layout:
.
├── manifest.json
├── data
│   ├── <OTA_file_sha256hash> # uncompressed file
│   ├── <OTA_file_sha256hash>.zst # zst compressed file
│   └── ...
└── meta
    ├── autoware
    │   └── <list of OTA metafiles...>
    ├── perception1
    │   └── <list of OTA metafiles...>
    └── perception2
        └── <list of OTA metafiles...>

Please refer to OTA cache design doc for more details.
"""


import argparse
import logging
import sys
import tempfile
from pathlib import Path

from .configs import cfg
from .builder import build
from .metadata import ImageMetadata

logger = logging.getLogger(__name__)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format=cfg.LOGGING_FORMAT, force=True)
    parser = argparse.ArgumentParser(
        prog="external_cache_builder",
        description=(
            "Helper script that builds external cache source"
            "image with given OTA images for offline OTA use."
        ),
    )
    parser.add_argument(
        "--image",
        help=(
            "--image=<ECU_NAME>:<IMAGE_PATH>[:<IMAGE_VERSION>], "
            "expect input image to be a tar archive(compressed or uncompressed),"
            "this option can be used multiple times to include multiple image."
        ),
        required=True,
        type=str,
        action="append",
    )
    parser.add_argument(
        "-o",
        "--output",
        help=(
            "-o <OUTPUT_PATH> or --output=<OUTPUT_PATH>,"
            "the path for generated external cache source tar archive."
        ),
        type=str,
        required=True,
    )
    args = parser.parse_args()
    image_metas = {}
    image_files = {}

    output = Path(args.output)
    if output.exists():
        logger.error(f"{output} exists, abort")
        sys.exit(-1)

    metadata_dir = Path(cfg.OUTPUT_META_DIR)
    for raw_pair in args.image:
        _parsed = str(raw_pair).split(":")
        if len(_parsed) == 2:  # no image version is included
            _ecu_id, _image_fpath = _parsed
            image_metas[_ecu_id] = ImageMetadata(
                image_name=Path(_image_fpath).name, meta_dir=str(metadata_dir / _ecu_id)
            )
            image_files[_ecu_id] = _image_fpath
        elif len(_parsed) == 3:
            _ecu_id, _image_fpath, _image_version = _parsed
            image_metas[_ecu_id] = ImageMetadata(
                image_name=Path(_image_fpath).name,
                image_version=_image_version,
                meta_dir=str(metadata_dir / _ecu_id),
            )
            image_files[_ecu_id] = _image_fpath
        else:
            logger.warning(f"ignore illegal image pair: {raw_pair}")

    if not image_metas:
        logger.error("at least one valid image should be given, abort")
        sys.exit(-1)

    with tempfile.TemporaryDirectory() as workdir:
        build(image_metas, image_files, workdir=workdir, output=output)
