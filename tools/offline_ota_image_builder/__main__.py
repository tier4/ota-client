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

Check README.md for the offline OTA image layout specification.
Please refer to OTA cache design doc for more details.
"""


import argparse
import errno
import logging
import sys
import tempfile
from pathlib import Path

from .builder import build
from .configs import cfg
from .manifest import ImageMetadata

logger = logging.getLogger(__name__)


def main(args):
    # ------ parse input image options ------ #
    image_metas = []
    image_files = {}

    for raw_pair in args.image:
        _parsed = str(raw_pair).split(":")
        if len(_parsed) >= 2:
            _ecu_id, _image_fpath, _image_version, *_ = *_parsed, ""
            image_metas.append(
                ImageMetadata(
                    ecu_id=_ecu_id,
                    image_version=_image_version,
                )
            )
            image_files[_ecu_id] = _image_fpath
        else:
            logger.warning(f"ignore illegal image pair: {raw_pair}")

    if not image_metas:
        print("ERR: at least one valid image should be given, abort")
        sys.exit(errno.EINVAL)

    # ------ parse export options ------ #
    output_fpath, write_to_dev = args.output, args.write_to

    if write_to_dev and not Path(write_to_dev).is_block_device():
        print(f"{write_to_dev} is not a block device, abort")
        sys.exit(errno.EINVAL)

    if write_to_dev and not args.force_write_to:
        _confirm_write_to = input(
            f"WARNING: generated image will be written to {write_to_dev}, \n"
            f"\t all data in {write_to_dev} will be lost, continue(type [Y] or [N]): "
        )
        if _confirm_write_to != "Y":
            print(f"decline writing to {write_to_dev}, abort")
            sys.exit(errno.EINVAL)
    elif write_to_dev and args.force_write_to:
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
        prog="offline_ota_image_builder",
        description=(
            "Helper script that builds offline OTA image "
            "with given OTA image(s) as external cache source or for offline OTA use."
        ),
    )
    parser.add_argument(
        "--image",
        help=(
            "OTA image for <ECU_ID> as tar archive(compressed or uncompressed), "
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
            "write the image to <DEVICE> and prepare the device as "
            "external cache source device."
        ),
        metavar="<DEVICE>",
    )
    parser.add_argument(
        "--force-write-to",
        help=(
            "prepare <DEVICE> as external cache source device without inter-active confirmation, "
            "only valid when used with -w option."
        ),
        action="store_true",
    )
    args = parser.parse_args()

    # basic options check
    if args.output and (output := Path(args.output)).is_file():
        print(f"ERR: {output} exists, abort")
        sys.exit(errno.EINVAL)

    if not (args.output or args.write_to):
        print("ERR: at least one export option should be specified")
        sys.exit(errno.EINVAL)

    try:
        main(args)
    except KeyboardInterrupt:
        print("ERR: aborted by user")
        raise
