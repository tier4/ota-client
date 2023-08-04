import logging
import os
import shutil
import tempfile
from contextlib import contextmanager
from os import PathLike, urandom
from pathlib import Path
from typing import Mapping, Union
from typing_extensions import TypeAlias

from otaclient.app.common import subprocess_call
from otaclient.app.ota_metadata import parse_regulars_from_txt

from .configs import cfg

logger = logging.getLogger(__name__)


class InputImageProcessError(Exception):
    ...


class ExportError(Exception):
    ...


StrPath: TypeAlias = Union[str, PathLike]


@contextmanager
def _unarchive_image(ecu_id: str, image_fpath: StrPath, *, workdir: StrPath):
    logger.info(f"unarchiving {image_fpath} for {ecu_id=} ...")
    with tempfile.TemporaryDirectory(dir=workdir) as tmp_dir:
        unarchive_dir = Path(tmp_dir) / ecu_id
        unarchive_dir.mkdir(exist_ok=True, parents=True)

        cmd = f"tar xf {image_fpath} -C {unarchive_dir}"
        try:
            subprocess_call(cmd)
        except Exception as e:
            _err_msg = f"failed to process input image {image_fpath}: {e!r}"
            logger.error(_err_msg)
            raise InputImageProcessError(_err_msg) from e

        logger.info(f"finish unarchiving {image_fpath} for {ecu_id=}")
        yield ecu_id, unarchive_dir


@contextmanager
def _create_output_image(image_workdir: StrPath, output_fpath: StrPath):
    data_dir = Path(image_workdir) / "data"
    meta_dir = Path(image_workdir) / "meta"
    data_dir.mkdir(exist_ok=True, parents=True)
    meta_dir.mkdir(exist_ok=True, parents=True)

    # NOTE: passthrough any errors during image processing,
    #       let the image processing task do the error handling.
    yield data_dir, meta_dir

    # export image_workdir as tar ball
    cmd = f"tar cf {output_fpath} -C {image_workdir} ."
    try:
        logger.info(f"exporting external cache source image to {output_fpath} ...")
        subprocess_call(cmd)
    except Exception as e:
        _err_msg = f"failed to export generated image to {output_fpath=}: {e!r}"
        logger.error(_err_msg)
        raise ExportError(_err_msg) from e


def _process_ota_image(
    ecu_id: str, ota_image_dir: StrPath, *, data_dir: StrPath, meta_dir: StrPath
):
    logger.info(f"processing OTA image for {ecu_id} ...")
    ota_image_dir = Path(ota_image_dir)

    # copy OTA metafiles
    ecu_metadir = Path(meta_dir) / ecu_id
    ecu_metadir.mkdir(parents=True, exist_ok=True)
    for _fname in cfg.OTA_METAFILES_LIST:
        _fpath = ota_image_dir / _fname
        shutil.copy(_fpath, ecu_metadir)

    # process OTA image files
    data_dir = Path(data_dir)

    ota_image_data_dir = ota_image_dir / "data"
    ota_image_data_zst_dir = ota_image_dir / "data.zst"
    _regulars_txt = ota_image_dir / "regulars.txt"
    with open(_regulars_txt, "r") as f:
        for line in f:
            reg_inf = parse_regulars_from_txt(line)
            ota_file_sha256 = reg_inf.sha256hash.hex()
            if reg_inf.compressed_alg == cfg.OTA_IMAGE_COMPRESSION_ALG:
                _zst_ota_fname = f"{ota_file_sha256}.{cfg.OTA_IMAGE_COMPRESSION_ALG}"
                src = str(ota_image_data_zst_dir / _zst_ota_fname)
                shutil.move(src, data_dir / _zst_ota_fname)
            else:
                src = str(ota_image_data_dir / os.path.relpath(reg_inf.path, "/"))
                shutil.move(src, data_dir / ota_file_sha256)

    logger.info(f"finish processing OTA image for {ecu_id}")


def build(image_pairs: Mapping[str, StrPath], *, workdir: StrPath, output: StrPath):
    image_workdir = Path(workdir) / f"output_{urandom(8).hex()}"
    image_workdir.mkdir(parents=True, exist_ok=True)
    manifest_json = image_workdir / "manifest.json"

    # TODO: define manifest format
    manifest_dic = {}
    with _create_output_image(image_workdir, output) as workdir_meta:
        data_dir, meta_dir = workdir_meta
        for ecu_id, image_fpath in image_pairs.items():
            with _unarchive_image(
                ecu_id, image_fpath, workdir=workdir
            ) as unarchived_meta:
                _, unarchive_dir = unarchived_meta
                _process_ota_image(
                    ecu_id, unarchive_dir, data_dir=data_dir, meta_dir=meta_dir
                )
