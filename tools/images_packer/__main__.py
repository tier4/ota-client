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


from __future__ import annotations
import argparse
import errno
import logging
import sys
import tempfile
from pathlib import Path
from typing import Callable

from .configs import cfg
from .builder import build
from .manifest import ImageMetadata

logger = logging.getLogger(__name__)


def main_build_offline_ota_image_bundle(args: argparse.Namespace):
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

    # ------ build image ------ #
    with tempfile.TemporaryDirectory(prefix="offline_OTA_image_builder") as workdir:
        build(
            image_metas,
            image_files,
            workdir=workdir,
            output=args.output,
        )


def main_build_external_cache_src(args: argparse.Namespace):
    # ------ args check ------ #
    if not (args.output or args.write_to):
        logger.error("ERR: at least one export option should be specified")
        sys.exit(errno.EINVAL)

    # ------ parse args ------ #
    image_metas: list[ImageMetadata] = []
    image_files: dict[str, Path] = {}
    count = 0

    # retrieves images from --image arg
    for _image_fpath in args.image:
        count_str = str(count)
        image_metas.append(ImageMetadata(ecu_id=count_str))
        image_files[count_str] = _image_fpath
        count += 1

    # retrieves images from --image-dir arg
    _image_store_dpath = Path(args.image_dir)
    if not _image_store_dpath.is_dir():
        logger.error(
            f"ERR: specified <IMAGE_FILES_DIR>={_image_store_dpath} doesn't exist, abort"
        )
        sys.exit(errno.EINVAL)

    for _image_fpath in _image_store_dpath.glob("*"):
        count_str = str(count)
        image_metas.append(ImageMetadata(ecu_id=count_str))
        image_files[count_str] = _image_fpath
        count += 1

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
        )


#
# ------ subcommands definition ------ #
#


def command_build_cache_src(
    subparsers: argparse._SubParsersAction,
) -> tuple[str, argparse.ArgumentParser]:
    cmd = "build-cache-src"
    subparser: argparse.ArgumentParser = subparsers.add_parser(
        name=cmd,
        description="build external OTA cache source recognized and used otaproxy.",
    )
    subparser.add_argument(
        "--image",
        help=(
            "(Optional) OTA image to be included in the external cache source, "
            "this argument can be specified multiple times to include multiple images."
        ),
        required=False,
        metavar="<IMAGE_FILE_PATH>",
        action="append",
    )
    subparser.add_argument(
        "--image-dir",
        help="(Optional) Specify a dir of OTA images to be included in the cache source.",
        required=False,
        metavar="<IMAGE_FILES_DIR>",
    )
    subparser.add_argument(
        "-w",
        "--write-to",
        help=(
            "(Optional) write the external cache source image to <DEVICE> and prepare the device, "
            "and then prepare the device to be used as external cache source storage."
        ),
        required=False,
        metavar="<DEVICE>",
    )
    subparser.add_argument(
        "--force-write-to",
        help=(
            "(Optional) prepare <DEVICE> as external cache source device without inter-active confirmation, "
            "only valid when used with -w option."
        ),
        required=False,
        action="store_true",
    )
    return cmd, subparser


def command_build_offline_ota_image_bundle(
    subparsers: argparse._SubParsersAction,
) -> tuple[str, argparse.ArgumentParser]:
    cmd = "build-offline-ota-imgs-bundle"
    subparser: argparse.ArgumentParser = subparsers.add_parser(
        name=cmd,
        description="build OTA image bundle for offline OTA use.",
    )
    subparser.add_argument(
        "--image",
        help=(
            "OTA image for <ECU_ID> as tar archive(compressed or uncompressed), "
            "this option can be used multiple times to include multiple images."
        ),
        required=True,
        metavar="<ECU_NAME>:<IMAGE_PATH>[:<IMAGE_VERSION>]",
        action="append",
    )
    return cmd, subparser


def register_handler(
    _cmd: str, subparser: argparse.ArgumentParser, *, handler: Callable
):
    subparser.set_defaults(handler=handler)


def get_handler(args: argparse.Namespace) -> Callable[[argparse.Namespace], None]:
    try:
        return args.handler
    except AttributeError:
        print("ERR: image packing mode is not specified, check -h for more details")
        sys.exit(errno.EINVAL)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format=cfg.LOGGING_FORMAT, force=True)

    # ------ init main parser ------ #
    main_parser = argparse.ArgumentParser(
        prog="images_packer",
        description=(
            "Helper script that builds OTA images bundle from given OTA image(s), "
            "start from choosing different image packing modes."
        ),
    )
    # shared args
    main_parser.add_argument(
        "-o",
        "--output",
        help="save the generated image bundle tar archive to <OUTPUT_FILE_PATH>.",
        metavar="<OUTPUT_FILE_PATH>",
    )

    # ------ define subcommands ------ #
    subparsers = main_parser.add_subparsers(
        title="Image packing modes",
    )
    register_handler(
        *command_build_cache_src(subparsers),
        handler=main_build_external_cache_src,
    )
    register_handler(
        *command_build_offline_ota_image_bundle(subparsers),
        handler=main_build_offline_ota_image_bundle,
    )

    args = main_parser.parse_args()

    # shared args check
    if args.output and (output := Path(args.output)).exists():
        print(f"ERR: {output} exists, abort")
        sys.exit(errno.EINVAL)

    try:
        get_handler(args)(args)
    except KeyboardInterrupt:
        print("ERR: aborted by user")
        raise
