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
            "--image=<ECU_NAME>:<IMAGE_PATH>, "
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
    ecu_image_pairs = {}

    output = Path(args.output)
    if output.exists():
        logger.error(f"{output} exists, abort")
        sys.exit(-1)

    for raw_pair in args.image:
        _parsed = str(raw_pair).split(":")
        if len(_parsed) != 2:
            logger.warning(f"ignore illegal image pair: {raw_pair}")
        _ecu_id, _image = _parsed
        ecu_image_pairs[_ecu_id] = _image
    if not ecu_image_pairs:
        logger.error("at least one valid image pair is required, abort")
        sys.exit(-1)

    with tempfile.TemporaryDirectory() as workdir:
        build(ecu_image_pairs, workdir=workdir, output=output)
