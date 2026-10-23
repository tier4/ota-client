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
"""Install the OTA config files shipped in the OTA image.

/boot/ota/ecu_info.yaml and /boot/ota/proxy_info.yaml hold the OTA settings of the ECU,
    including the addresses the ECUs use to reach each other. At each OTA, the copies
    shipped in the OTA image are installed, so that a change to these settings in the
    OTA image reaches the ECU. A file listed in the persist files of the OTA image
    is not installed, the current one is kept for it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

from otaclient.configs._ecu_info import load_ecu_info
from otaclient.configs._proxy_info import load_proxy_info
from otaclient.configs.cfg import cfg
from otaclient_common._io import write_str_to_file_atomic

logger = logging.getLogger(__name__)

ECU_INFO_FNAME = Path(cfg.ECU_INFO_FPATH).name
PROXY_INFO_FNAME = Path(cfg.PROXY_INFO_FPATH).name


def _is_persisted(fpath: PurePosixPath, persist_entries: Iterable[str]) -> bool:
    """Whether <fpath> is listed in the persist files, by itself or by one of its parents."""
    for _entry in persist_entries:
        _entry_path = PurePosixPath(os.path.normpath(_entry))
        if _entry_path == fpath or _entry_path in fpath.parents:
            return True
    return False


def load_ota_config_files_from_image(
    image_ota_dpath: Path, *, persist_entries: Iterable[str] = ()
) -> dict[str, str]:
    """Load the OTA config files shipped in the OTA image.

    This must be called before the persist files handling, which replaces the
        persisted files in the OTA image with the active slot's ones.

    Args:
        image_ota_dpath: the /boot/ota folder of the OTA image.
        persist_entries: the persist files of the OTA image. A file listed in it
            is not loaded, so that the current one is kept.

    Returns:
        A dict maps the file name to the file content. Files not shipped in
            the OTA image as regular files, or listed in the persist files,
            are not included.
    """
    if image_ota_dpath.is_symlink():
        logger.warning(f"{image_ota_dpath} in the OTA image is a symlink, ignored")
        return {}

    _persist_entries = list(persist_entries)
    res: dict[str, str] = {}
    for _fname in (ECU_INFO_FNAME, PROXY_INFO_FNAME):
        if _is_persisted(PurePosixPath(cfg.OTA_DPATH) / _fname, _persist_entries):
            logger.info(
                f"{_fname} is listed in the persist files, keep the current one"
            )
            continue

        _fpath = image_ota_dpath / _fname
        # NOTE: not following the symlink, which is resolved against the running
        #       rootfs instead of the OTA image.
        if _fpath.is_symlink():
            logger.warning(f"{_fpath} in the OTA image is a symlink, ignored")
            continue
        if _fpath.is_file():
            res[_fname] = _fpath.read_text()
        else:
            logger.info(f"no {_fname} in the OTA image, keep the current one")
    return res


def _load_current_ecu_id(current_ecu_info_fpath: Path) -> str | None:
    try:
        return load_ecu_info(current_ecu_info_fpath.read_text()).ecu_id
    except Exception as e:
        # NOTE: otaclient rejects OTA requests when it starts without a valid
        #       ecu_info.yaml, so this only happens when the file is changed
        #       after otaclient started.
        logger.warning(
            f"failed to load the current {current_ecu_info_fpath}: {e!r}, "
            "skip the ecu_id check"
        )
        return None


def _install(dst_fpath: Path, content: str) -> None:
    logger.info(f"install {dst_fpath.name} from the OTA image to {dst_fpath}")
    # NOTE: replace the symlink at <dst_fpath> instead of writing to its target.
    write_str_to_file_atomic(dst_fpath, content, follow_symlink=False)


def install_ota_config_files(
    image_ota_config_files: Mapping[str, str],
    *,
    dst_ota_dpath: Path,
    current_ecu_info_fpath: Path,
) -> None:
    """Install the OTA config files shipped in the OTA image to <dst_ota_dpath>.

    Nothing is installed when the ecu_id of the OTA image's ecu_info.yaml differs
        from the current ecu_info.yaml's, as the OTA image is then not for this ECU,
        and the ecu_id of an ECU is not expected to be changed via OTA.

    Otherwise, each file is installed unless it is not valid.

    A file that is not installed, not shipped in the OTA image, or listed in
        the persist files is left as it is at <dst_ota_dpath>.

    Args:
        image_ota_config_files: the files loaded by load_ota_config_files_from_image.
        dst_ota_dpath: the /boot/ota folder that will be used by the next boot.
        current_ecu_info_fpath: the ecu_info.yaml currently in use.
    """
    _ecu_info = image_ota_config_files.get(ECU_INFO_FNAME)
    _proxy_info = image_ota_config_files.get(PROXY_INFO_FNAME)

    if _ecu_info is not None:
        try:
            _new_ecu_id = load_ecu_info(_ecu_info).ecu_id
        except Exception as e:
            logger.warning(
                f"ecu_info.yaml in the OTA image is invalid, keep the current one: {e!r}"
            )
            _ecu_info = None
        else:
            _current_ecu_id = _load_current_ecu_id(current_ecu_info_fpath)
            if _current_ecu_id is not None and _current_ecu_id != _new_ecu_id:
                logger.warning(
                    f"ecu_id in the OTA image's ecu_info.yaml({_new_ecu_id}) differs from "
                    f"the current one({_current_ecu_id}), the OTA image is not for this ECU, "
                    "keep the current ecu_info.yaml and proxy_info.yaml"
                )
                return

    if _proxy_info is not None:
        try:
            load_proxy_info(_proxy_info)
        except Exception as e:
            logger.warning(
                f"proxy_info.yaml in the OTA image is invalid, keep the current one: {e!r}"
            )
            _proxy_info = None

    dst_ota_dpath.mkdir(exist_ok=True, parents=True)
    if _ecu_info is not None:
        _install(dst_ota_dpath / ECU_INFO_FNAME, _ecu_info)
    if _proxy_info is not None:
        _install(dst_ota_dpath / PROXY_INFO_FNAME, _proxy_info)
