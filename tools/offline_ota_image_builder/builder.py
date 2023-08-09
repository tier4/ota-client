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

from otaclient.app.common import subprocess_call
from otaclient.app.ota_metadata import parse_regulars_from_txt

from .configs import cfg
from .manifest import ImageMetadata, Manifest
from .utils import StrPath, InputImageProcessError, ExportError

logger = logging.getLogger(__name__)

PROC_MOUNTS = "/proc/mounts"


def _check_if_mounted(dev: StrPath):
    for line in Path(PROC_MOUNTS).read_text().splitlines():
        if line.find(str(dev)) != -1:
            return True
    return False


@contextmanager
def _unarchive_image(image_fpath: StrPath, *, workdir: StrPath):
    _start_time = time.time()
    with tempfile.TemporaryDirectory(dir=workdir) as unarchive_dir:
        cmd = f"tar xf {image_fpath} -C {unarchive_dir}"
        try:
            subprocess_call(cmd)
        except Exception as e:
            _err_msg = f"failed to process input image {image_fpath}: {e!r}, {cmd=}"
            logger.error(_err_msg)
            raise InputImageProcessError(_err_msg) from e

        logger.info(
            f"finish unarchiving {image_fpath}, takes {time.time() - _start_time:.2f}s"
        )
        yield unarchive_dir


def _process_ota_image(ota_image_dir: StrPath, *, data_dir: StrPath, meta_dir: StrPath):
    _start_time = time.time()
    data_dir = Path(data_dir)
    # statistics
    saved_files, saved_files_size = 0, 0

    # process OTA image files, save to shared data folder
    ota_image_dir = Path(ota_image_dir)
    ota_image_data_dir = ota_image_dir / cfg.OTA_IMAGE_DATA_DIR
    ota_image_data_zst_dir = ota_image_dir / cfg.OTA_IMAGE_DATA_ZST_DIR
    with open(ota_image_dir / cfg.OTA_METAFILE_REGULAR, "r") as f:
        for line in f:
            reg_inf = parse_regulars_from_txt(line)
            ota_file_sha256 = reg_inf.sha256hash.hex()

            if reg_inf.compressed_alg == cfg.OTA_IMAGE_COMPRESSION_ALG:
                _zst_ota_fname = f"{ota_file_sha256}.{cfg.OTA_IMAGE_COMPRESSION_ALG}"
                src = ota_image_data_zst_dir / _zst_ota_fname
                dst = data_dir / _zst_ota_fname
                # NOTE: multiple OTA files might have the same hash
                # NOTE: squash OTA file permission before moving
                if not dst.is_file() and src.is_file():
                    src.chmod(0o644)
                    saved_files += 1
                    shutil.move(str(src), dst)
                    saved_files_size += dst.stat().st_size

            else:
                src = ota_image_data_dir / os.path.relpath(reg_inf.path, "/")
                dst = data_dir / ota_file_sha256
                if not dst.is_file() and src.is_file():
                    src.chmod(0o644)
                    saved_files += 1
                    shutil.move(str(src), dst)
                    saved_files_size += dst.stat().st_size

    # copy OTA metafiles to image specific meta folder
    for _fname in cfg.OTA_METAFILES_LIST:
        shutil.move(str(ota_image_dir / _fname), meta_dir)

    logger.info(f"finish processing OTA image, takes {time.time() - _start_time:.2f}s")
    return saved_files, saved_files_size


def _create_image_tar(image_rootfs: StrPath, output_fpath: StrPath):
    """Export generated image rootfs as tar ball."""
    cmd = f"tar cf {output_fpath} -C {image_rootfs} ."
    try:
        logger.info(f"exporting external cache source image to {output_fpath} ...")
        subprocess_call(cmd)
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
    format_device_cmd = f"mkfs.ext4 -L {cfg.EXTERNAL_CACHE_DEV_FSLABEL} {dev}"
    try:
        logger.warning(f"formatting {dev} to ext4: {format_device_cmd}")
        # NOTE: label offline OTA image as external cache source
        subprocess_call(format_device_cmd)
    except Exception as e:
        _err_msg = f"failed to formatting {dev} to ext4: {e!r}"
        logger.error(_err_msg)
        raise ExportError(_err_msg) from e

    # mount and copy
    mount_point = Path(workdir) / "mnt"
    mount_point.mkdir(exist_ok=True)
    cp_cmd = f"cp -r {str(image_rootfs).rstrip('/')}/. -t {mount_point}"
    try:
        logger.info(f"copying image rootfs to {dev=}@{mount_point=}...")
        subprocess_call(f"mount --make-private --make-unbindable {dev} {mount_point}")
        subprocess_call(cp_cmd)

        os.sync()
        logger.info(f"finish copying, takes {time.time()-_start_time:.2f}s")
    except Exception as e:
        _err_msg = f"failed to export to image rootfs to {dev=}@{mount_point=}: {e!r}, {cp_cmd=}"
        logger.error(_err_msg)
        raise ExportError(_err_msg) from e
    finally:
        subprocess_call(f"umount -l {dev}", raise_exception=False)


def build(
    image_metas: Sequence[ImageMetadata],
    image_files: Mapping[str, StrPath],
    *,
    workdir: StrPath,
    output: Optional[StrPath],
    write_to_dev: Optional[StrPath],
):
    _start_time = time.time()
    logger.info(f"job started at {int(_start_time)}")

    output_workdir = Path(workdir) / cfg.OUTPUT_WORKDIR
    output_workdir.mkdir(parents=True, exist_ok=True)
    output_data_dir = output_workdir / cfg.OUTPUT_DATA_DIR
    output_data_dir.mkdir(exist_ok=True, parents=True)
    output_meta_dir = output_workdir / cfg.OUTPUT_META_DIR
    output_meta_dir.mkdir(exist_ok=True, parents=True)

    # ------ generate image ------ #
    manifest = Manifest(image_meta=list(image_metas))
    # process all inpu OTA images
    images_unarchiving_work_dir = Path(workdir) / cfg.IMAGE_UNARCHIVE_WORKDIR
    images_unarchiving_work_dir.mkdir(parents=True, exist_ok=True)
    for idx, image_meta in enumerate(manifest.image_meta):
        ecu_id = image_meta.ecu_id
        image_file = image_files[ecu_id]
        # image specific metadata dir
        _meta_dir = output_meta_dir / str(idx)
        _meta_dir.mkdir(exist_ok=True, parents=True)

        logger.info(f"{ecu_id=}: unarchive {image_file} ...")
        with _unarchive_image(
            image_file, workdir=images_unarchiving_work_dir
        ) as unarchived_image_dir:
            logger.info(f"{ecu_id=}: processing OTA image ...")
            _saved_files_num, _saved_files_size = _process_ota_image(
                unarchived_image_dir,
                data_dir=output_data_dir,
                meta_dir=_meta_dir,
            )
            # update manifest after this image is processed
            manifest.data_size += _saved_files_size
            manifest.data_files_num += _saved_files_num

    # process metadata
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
    if output:
        _create_image_tar(output_workdir, output_fpath=output)
    if write_to_dev:
        _write_image_to_dev(output_workdir, dev=write_to_dev, workdir=workdir)
    logger.info(f"job finished, takes {time.time() - _start_time:.2f}s")
