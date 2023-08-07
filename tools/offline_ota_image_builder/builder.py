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
import shutil
import tempfile
import time
from contextlib import contextmanager
from os import PathLike
from pathlib import Path
from typing import Mapping, Union
from typing_extensions import TypeAlias

from otaclient.app.common import subprocess_call
from otaclient.app.ota_metadata import parse_regulars_from_txt

from .configs import cfg
from .manifest import ImageMetadata, Manifest

logger = logging.getLogger(__name__)


class InputImageProcessError(Exception):
    ...


class ExportError(Exception):
    ...


StrPath: TypeAlias = Union[str, PathLike]


@contextmanager
def _unarchive_image(ecu_id: str, image_fpath: StrPath, *, workdir: StrPath):
    _start_time = time.time()
    logger.info(f"{ecu_id=}: unarchiving {image_fpath} ...")
    with tempfile.TemporaryDirectory(prefix=f"{ecu_id}_", dir=workdir) as unarchive_dir:
        cmd = f"tar xf {image_fpath} -C {unarchive_dir}"
        try:
            subprocess_call(cmd)
        except Exception as e:
            _err_msg = f"failed to process input image {image_fpath}: {e!r}"
            logger.error(_err_msg)
            raise InputImageProcessError(_err_msg) from e

        logger.info(
            f"{ecu_id=}: finish unarchiving {image_fpath}, takes {time.time() - _start_time:.2f}s"
        )
        yield ecu_id, unarchive_dir


@contextmanager
def _create_output_image(output_workdir: StrPath, output_fpath: StrPath):
    _start_time = time.time()
    data_dir = Path(output_workdir) / cfg.OUTPUT_DATA_DIR
    meta_dir = Path(output_workdir) / cfg.OUTPUT_META_DIR
    data_dir.mkdir(exist_ok=True, parents=True)
    meta_dir.mkdir(exist_ok=True, parents=True)

    # NOTE: passthrough any errors during image processing,
    #       let the image processing task do the error handling.
    yield data_dir, meta_dir

    # export image_workdir as tar ball
    cmd = f"tar cf {output_fpath} -C {output_workdir} ."
    try:
        logger.info(f"exporting external cache source image to {output_fpath} ...")
        subprocess_call(cmd)
        logger.info(
            f"finish creating offline OTA image: takes {time.time() - _start_time:.2f}s"
        )
    except Exception as e:
        _err_msg = f"failed to export generated image to {output_fpath=}: {e!r}"
        logger.error(_err_msg)
        raise ExportError(_err_msg) from e


def _process_ota_image(
    ecu_id: str, ota_image_dir: StrPath, *, data_dir: StrPath, meta_dir: StrPath
):
    # TODO: parse metadata.jwt to check metadata version
    _start_time = time.time()
    logger.info(f"{ecu_id=}: processing OTA image ...")
    data_dir = Path(data_dir)
    # statistics
    saved_files, processed_size = 0, 0

    # process OTA image files
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
                if not dst.is_file():
                    saved_files += 1
                    processed_size += src.stat().st_size
                    shutil.move(str(src), dst)
            else:
                src = ota_image_data_dir / os.path.relpath(reg_inf.path, "/")
                dst = data_dir / ota_file_sha256
                if not dst.is_file():
                    saved_files += 1
                    processed_size += src.stat().st_size
                    shutil.move(str(src), dst)

    # copy OTA metafiles
    # NOTE: also add the metafiles' size to the processed_size
    ecu_metadir = Path(meta_dir) / ecu_id
    ecu_metadir.mkdir(parents=True, exist_ok=True)
    for _fname in cfg.OTA_METAFILES_LIST:
        _fpath = ota_image_dir / _fname
        processed_size += _fpath.stat().st_size
        shutil.copy(_fpath, ecu_metadir)

    logger.info(
        f"{ecu_id=}: finish processing OTA image, takes {time.time() - _start_time:.2f}s"
    )
    return saved_files, processed_size


def build(
    image_metas: Mapping[str, ImageMetadata],
    image_files: Mapping[str, StrPath],
    *,
    workdir: StrPath,
    output: StrPath,
):
    _start_time = time.time()
    logger.info(f"build started at {int(_start_time)}")

    output_workdir = Path(workdir) / cfg.OUTPUT_WORKDIR
    output_workdir.mkdir(parents=True, exist_ok=True)
    manifest_json = output_workdir / cfg.MANIFEST_JSON

    manifest = Manifest()
    with _create_output_image(output_workdir, output) as output_meta:
        output_data_dir, output_meta_dir = output_meta

        # process all inpu OTA images
        images_unarchiving_work_dir = Path(workdir) / cfg.IMAGE_UNARCHIVE_WORKDIR
        images_unarchiving_work_dir.mkdir(parents=True, exist_ok=True)
        for ecu_id, image_meta in image_metas.items():
            manifest.ecu_ids.append(ecu_id)
            manifest.image_metadata[ecu_id] = image_meta

            with _unarchive_image(
                ecu_id, image_files[ecu_id], workdir=images_unarchiving_work_dir
            ) as _image_unarchived_meta:
                _, unarchived_image_dir = _image_unarchived_meta
                _saved_files_num, _processed_size = _process_ota_image(
                    ecu_id,
                    unarchived_image_dir,
                    data_dir=output_data_dir,
                    meta_dir=output_meta_dir,
                )
                # update manifest after this image is processed
                manifest.image_size += _processed_size
                manifest.total_files_num += _saved_files_num

        # write offline OTA image manifest
        manifest_json.write_text(manifest.export_to_json())

    logger.info(f"build finished, takes {time.time() - _start_time:.2f}s")
    logger.info(f"build manifest: {manifest}")
