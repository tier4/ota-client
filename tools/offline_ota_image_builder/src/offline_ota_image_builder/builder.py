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


import logging
import os
import pprint
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Mapping, Optional, Sequence

from offline_ota_image_builder.ota_metadata.v1 import (
    DB_FNAME,
    OTAImageHelper,
    iter_resource_table,
)

from .configs import cfg
from .manifest import ImageMetadata, Manifest
from .ota_metadata import legacy as ota_metadata_parser
from .utils import (
    ExportError,
    InputImageProcessError,
    StrPath,
    exit_with_err_msg,
    subprocess_run_wrapper,
)

logger = logging.getLogger(__name__)

PROC_MOUNTS = "/proc/mounts"


def _check_if_mounted(dev: StrPath):
    for line in Path(PROC_MOUNTS).read_text().splitlines():
        if line.strip().split()[0] == str(dev):
            return True
    return False


@contextmanager
def _unarchive_image(image_fpath: StrPath, *, workdir: StrPath):
    _start_time = time.time()
    with tempfile.TemporaryDirectory(dir=workdir) as unarchive_dir:
        cmd = [
            "tar",
            "-xf",
            str(image_fpath),
            "--no-same-owner",
            "-C",
            str(unarchive_dir),
        ]
        try:
            subprocess_run_wrapper(cmd)
        except Exception as e:
            _err_msg = f"failed to process input image {image_fpath}: {e!r}, {cmd=}"
            logger.error(_err_msg)
            raise InputImageProcessError(_err_msg) from e

        logger.info(
            f"finish unarchiving {image_fpath}, takes {time.time() - _start_time:.2f}s"
        )
        yield unarchive_dir


def _process_legacy_ota_image(
    ota_image_dir: StrPath, *, data_dir: StrPath, meta_dir: StrPath
):
    """Processing OTA image under <ota_image_dir> and update <data_dir> and <meta_dir>."""
    data_dir = Path(data_dir)
    # statistics
    saved_files, saved_files_size = 0, 0

    ota_image_dir = Path(ota_image_dir)

    # ------ process OTA image metadata ------ #
    metadata_jwt_fpath = ota_image_dir / ota_metadata_parser.METADATA_JWT
    # NOTE: we don't need to do certificate verification here, so set certs_dir to empty
    metadata_jwt = ota_metadata_parser.MetadataJWTParser(
        metadata_jwt_fpath.read_text(), certs_dir=""
    ).get_otametadata()

    rootfs_dir = ota_image_dir / metadata_jwt.rootfs_directory
    compressed_rootfs_dir = ota_image_dir / metadata_jwt.compressed_rootfs_directory

    # ------ update data_dir with the contents of this OTA image ------ #
    with open(ota_image_dir / metadata_jwt.regular.file, "r") as f:
        for line in f:
            reg_inf = ota_metadata_parser.parse_regulars_from_txt(line)
            ota_file_sha256 = reg_inf.sha256hash.hex()

            if reg_inf.compressed_alg == cfg.OTA_IMAGE_COMPRESSION_ALG:
                _zst_ota_fname = f"{ota_file_sha256}.{cfg.OTA_IMAGE_COMPRESSION_ALG}"
                src = compressed_rootfs_dir / _zst_ota_fname
                dst = data_dir / _zst_ota_fname
                # NOTE: multiple OTA files might have the same hash
                # NOTE: squash OTA file permission before moving
                if not dst.is_file() and src.is_file():
                    src.chmod(0o644)
                    saved_files += 1
                    shutil.move(str(src), dst)
                    saved_files_size += dst.stat().st_size

            else:
                src = rootfs_dir / os.path.relpath(reg_inf.path, "/")
                dst = data_dir / ota_file_sha256
                if not dst.is_file() and src.is_file():
                    src.chmod(0o644)
                    saved_files += 1
                    shutil.move(str(src), dst)
                    saved_files_size += dst.stat().st_size

    # ------ update meta_dir with the OTA meta files in this image ------ #
    # copy OTA metafiles to image specific meta folder
    for _metaf in ota_metadata_parser.MetafilesV1:
        shutil.move(str(ota_image_dir / _metaf.value), meta_dir)
    # copy certificate and metadata.jwt
    shutil.move(str(ota_image_dir / metadata_jwt.certificate.file), meta_dir)
    shutil.move(str(metadata_jwt_fpath), meta_dir)

    return saved_files, saved_files_size


def _create_image_tar(image_rootfs: StrPath, output_fpath: StrPath):
    """Export generated image rootfs as tar ball."""
    cmd = ["tar", "cf", str(output_fpath), "-C", str(image_rootfs), "."]
    try:
        logger.info(f"exporting external cache source image to {output_fpath} ...")
        subprocess_run_wrapper(cmd)
    except Exception as e:
        _err_msg = f"failed to tar generated image to {output_fpath=}: {e!r}, {cmd=}"
        logger.error(_err_msg)
        raise ExportError(_err_msg) from e


def _write_image_to_dev(image_rootfs: StrPath, dev: StrPath, *, workdir: StrPath):
    """Prepare <WRITE_TO_DEV> and export generated image's rootfs to it."""
    _start_time = time.time()
    dev = Path(dev)
    if not dev.is_block_device():
        logger.warning(f"{dev=} is not a block device, skip")
        return
    if _check_if_mounted(dev):
        logger.warning(f"{dev=} is mounted, skip")
        return

    # prepare device
    format_device_cmd = [
        "mkfs.ext4",
        "-L",
        cfg.EXTERNAL_CACHE_DEV_FSLABEL,
        str(dev),
    ]
    try:
        logger.warning(f"formatting {dev} to ext4: {format_device_cmd}")
        # NOTE: label offline OTA image as external cache source
        subprocess_run_wrapper(format_device_cmd)
    except Exception as e:
        _err_msg = f"failed to formatting {dev} to ext4: {e!r}"
        logger.error(_err_msg)
        raise ExportError(_err_msg) from e

    # mount and copy
    mount_point = Path(workdir) / "mnt"
    mount_point.mkdir(exist_ok=True)
    cp_cmd = ["cp", "-rT", str(image_rootfs), str(mount_point)]
    try:
        logger.info(f"copying image rootfs to {dev=}@{mount_point=}...")
        subprocess_run_wrapper(
            ["mount", "--make-private", "--make-unbindable", str(dev), str(mount_point)]
        )
        subprocess_run_wrapper(cp_cmd)
        logger.info(f"finish copying, takes {time.time() - _start_time:.2f}s")
    except Exception as e:
        _err_msg = f"failed to export to image rootfs to {dev=}@{mount_point=}: {e!r}, {cp_cmd=}"
        logger.error(_err_msg)
        raise ExportError(_err_msg) from e
    finally:
        subprocess_run_wrapper(["umount", "-l", str(dev)], check=False)


def _build_one_legacy(
    idx: int,
    image_file: StrPath,
    *,
    workdir: Path,
    output_meta_dir: Path,
    output_data_dir: Path,
) -> tuple[int, int]:
    # image specific metadata dir
    _meta_dir = output_meta_dir / str(idx)
    _meta_dir.mkdir(exist_ok=True, parents=True)

    with _unarchive_image(image_file, workdir=workdir) as unarchived_image_dir:
        _saved_files_num, _saved_files_size = _process_legacy_ota_image(
            unarchived_image_dir,
            data_dir=output_data_dir,
            meta_dir=_meta_dir,
        )
        return _saved_files_size, _saved_files_num


def _build_one_v1(
    idx: int,
    image_file: StrPath,
    *,
    workdir: Path,
    output_meta_dir: Path,
    output_data_dir: Path,
) -> tuple[int, int]:
    _saved_files_size, _saved_files_num = 0, 0

    # image specific metadata dir
    _meta_dir = output_meta_dir / str(idx)
    _meta_dir.mkdir(exist_ok=True, parents=True)

    _rst_file = workdir / DB_FNAME
    _image_helper = OTAImageHelper(Path(image_file))
    _image_helper.save_resource_table(_rst_file)

    # save a copy of index.json to the metadir
    _image_helper.save_index_json(_meta_dir / "index.json")

    for _entry_digest, _is_compressed in iter_resource_table(_rst_file):
        if not _image_helper.lookup_and_check_blob(_entry_digest):
            # not a leaf blobs in the blob storage or has already been checked
            continue

        _entry_digest_hex = _entry_digest.hex()
        if _is_compressed:
            _save_dst = (
                output_data_dir / f"{_entry_digest_hex}.{cfg.OTA_IMAGE_COMPRESSION_ALG}"
            )
        else:
            _save_dst = output_data_dir / _entry_digest_hex

        _saved_files_size += _image_helper.save_blob(_entry_digest_hex, _save_dst)
        _saved_files_num += 1
    return _saved_files_size, _saved_files_num


def build(
    image_metas: Sequence[ImageMetadata],
    image_files: Mapping[str, StrPath],
    *,
    workdir: StrPath,
    output: Optional[StrPath],
    write_to_dev: Optional[StrPath],
) -> None:
    _start_time = time.time()
    logger.info(f"job started at {int(_start_time)}")

    output_workdir = Path(workdir) / cfg.OUTPUT_WORKDIR
    output_workdir.mkdir(parents=True, exist_ok=True)
    output_data_dir = output_workdir / cfg.OUTPUT_DATA_DIR
    output_data_dir.mkdir(exist_ok=True, parents=True)
    output_meta_dir = output_workdir / cfg.OUTPUT_META_DIR
    output_meta_dir.mkdir(exist_ok=True, parents=True)
    manifest = Manifest(image_meta=list(image_metas))

    images_unarchiving_work_dir = Path(workdir) / cfg.IMAGE_UNARCHIVE_WORKDIR
    images_unarchiving_work_dir.mkdir(parents=True, exist_ok=True)

    # ------ generate image ------ #
    for idx, image_meta in enumerate(manifest.image_meta):
        _start = time.time()
        ecu_id = image_meta.ecu_id
        image_file = Path(image_files[ecu_id])

        if not image_file.is_file():
            exit_with_err_msg(f"{ecu_id=}: specified {image_file=} not found!")

        if image_file.suffix.lower() == ".zip":
            logger.info(f"{ecu_id=}: processing OTA image in v1 format ...")
            _saved_files_size, _saved_files_num = _build_one_v1(
                idx=idx,
                image_file=image_file,
                workdir=images_unarchiving_work_dir,
                output_meta_dir=output_meta_dir,
                output_data_dir=output_data_dir,
            )
        else:
            logger.info(f"{ecu_id=}: process a legacy format OTA image ...")
            _saved_files_size, _saved_files_num = _build_one_legacy(
                idx=idx,
                image_file=image_file,
                workdir=images_unarchiving_work_dir,
                output_meta_dir=output_meta_dir,
                output_data_dir=output_data_dir,
            )

        logger.info(
            f"{ecu_id=}: finish processing image, time cost: {time.time() - _start}s"
        )

        manifest.data_size += _saved_files_size
        manifest.data_files_num += _saved_files_num

    # ------ process metadata ------ #
    for cur_path, _, fnames in os.walk(output_meta_dir):
        _cur_path = Path(cur_path)
        for _fname in fnames:
            _fpath = _cur_path / _fname
            if _fpath.is_file():
                manifest.meta_size += _fpath.stat().st_size

    # write offline OTA image manifest
    manifest.build_timestamp = int(time.time())
    Path(output_workdir / cfg.MANIFEST_JSON).write_text(manifest.export_to_json())
    logger.info(f"build manifest: {pprint.pformat(manifest)}")

    # ------ export image ------ #
    os.sync()  # make sure all changes are saved to disk before export
    try:
        if output:
            _create_image_tar(output_workdir, output_fpath=output)
        if write_to_dev:
            _write_image_to_dev(output_workdir, dev=write_to_dev, workdir=workdir)
    except Exception:
        exit_with_err_msg("failed to save the result!")

    logger.info(f"job finished, takes {time.time() - _start_time:.2f}s")
