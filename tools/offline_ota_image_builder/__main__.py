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
from .manifest import ImageMetadata

logger = logging.getLogger(__name__)


def main(args):
    # ------ parse input image options ------ #
    image_metas = {}
    image_files = {}

    metadata_dir = Path(cfg.OUTPUT_META_DIR)
    for raw_pair in args.image:
        _parsed = str(raw_pair).split(":")
        if len(_parsed) >= 2:
            _ecu_id, _image_fpath, _image_version, *_ = *_parsed, ""
            image_metas[_ecu_id] = ImageMetadata(
                ecu_id=_ecu_id,
                image_version=_image_version,
                meta_dir=str(metadata_dir / _ecu_id),
            )
            image_files[_ecu_id] = _image_fpath
        else:
            logger.warning(f"ignore illegal image pair: {raw_pair}")

    if not image_metas:
        print("ERR: at least one valid image should be given, abort")
        sys.exit(-1)

    # ------ parse export options ------ #
    output_fpath, write_to_dev = args.output, args.write_to

    if write_to_dev and not Path(write_to_dev).is_block_device():
        print(f"{write_to_dev} is not a block device, abort")
        sys.exit(-1)

    if write_to_dev and not args.confirm_write_to:
        _confirm_write_to = input(
            f"WARNING: generated image will be written to {write_to_dev}, \n"
            f"\t all data in {write_to_dev} will be lost, continue(type [Y] or [N]): "
        )
        if _confirm_write_to != "Y":
            print(f"decline writing to {write_to_dev}, abort")
            sys.exit(-1)
    elif write_to_dev and args.confirm_write_to:
        logger.warning(
            f"generated image will be written to {write_to_dev},"
            f"all data in {write_to_dev} will be lost"
        )

    # ------ build image ------ #
    with tempfile.TemporaryDirectory(prefix="offline_OTA_image_builder") as workdir:
        build(
            image_metas,
            image_files,
            workdir=workdir,
            output=output_fpath,
            write_to_dev=write_to_dev,
        )


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
            "expect input image to be a tar archive(compressed or uncompressed),"
            "this option can be used multiple times to include multiple images."
        ),
        required=True,
        metavar="<ECU_NAME>:<IMAGE_PATH>[:<IMAGE_VERSION>]",
        action="append",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="save the generated image rootfs into tar archive to <OUTPUT_PATH>.",
        metavar="<OUTPUT_PATH>",
    )
    parser.add_argument(
        "-w",
        "--write-to",
        help=(
            "write the image to <DEVICE> and prepare the device as"
            "external cache source device."
        ),
        metavar="<DEVICE>",
    )
    parser.add_argument(
        "--confirm-write-to",
        help=(
            "writing generated to <DEVICE> without inter-active confirmation,"
            "only valid when used with -w option"
        ),
        action="store_true",
    )
    args = parser.parse_args()

    # basic options check
    output = Path(args.output)
    if output.exists():
        print(f"ERR: {output} exists, abort")
        sys.exit(-1)

    if not (args.output or args.write_to):
        print("ERR: at least one export option should be specified")
        sys.exit(-1)

    try:
        main(args)
    except KeyboardInterrupt:
        print("ERR: aborted by user")
        sys.exit(-1)
