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
    OTA image reaches the ECU.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from otaclient.configs import ECUInfo
from otaclient.configs.cfg import cfg
from otaclient_common._io import write_str_to_file_atomic

logger = logging.getLogger(__name__)

ECU_INFO_FNAME = Path(cfg.ECU_INFO_FPATH).name
PROXY_INFO_FNAME = Path(cfg.PROXY_INFO_FPATH).name


def load_ota_config_files_from_image(image_ota_dpath: Path) -> dict[str, str]:
    """Load the OTA config files shipped in the OTA image.

    This must be called before the /boot/ota folder of the OTA image is replaced
        with the active slot's one.

    Returns:
        A dict maps the file name to the file content. Files not shipped in
            the OTA image are not included.
    """
    res: dict[str, str] = {}
    for _fname in (ECU_INFO_FNAME, PROXY_INFO_FNAME):
        _fpath = image_ota_dpath / _fname
        if _fpath.is_file():
            res[_fname] = _fpath.read_text()
    return res


def _load_ecu_id(ecu_info_yaml: str) -> str:
    _loaded = yaml.safe_load(ecu_info_yaml)
    assert isinstance(_loaded, dict), "not a valid yaml file"
    return ECUInfo.model_validate(_loaded, strict=True).ecu_id


def install_ota_config_files(
    image_ota_config_files: dict[str, str],
    *,
    dst_ota_dpath: Path,
    current_ecu_info_fpath: Path,
) -> None:
    """Install the OTA config files shipped in the OTA image to <dst_ota_dpath>.

    proxy_info.yaml from the OTA image is always installed.

    ecu_info.yaml from the OTA image is installed, except when:
    1. it is not a valid ecu_info.yaml, or
    2. its ecu_id differs from the current ecu_info.yaml's, as the ecu_id of
        an ECU is not expected to be changed via OTA.
    In these cases, the ecu_info.yaml at <dst_ota_dpath> is left as it is.

    A file not shipped in the OTA image is left as it is.

    Args:
        image_ota_config_files: the files loaded by load_ota_config_files_from_image.
        dst_ota_dpath: the /boot/ota folder that will be used by the next boot.
        current_ecu_info_fpath: the ecu_info.yaml currently in use.
    """
    dst_ota_dpath.mkdir(exist_ok=True, parents=True)

    if (_proxy_info := image_ota_config_files.get(PROXY_INFO_FNAME)) is not None:
        logger.info("install proxy_info.yaml from the OTA image")
        write_str_to_file_atomic(dst_ota_dpath / PROXY_INFO_FNAME, _proxy_info)

    if (_ecu_info := image_ota_config_files.get(ECU_INFO_FNAME)) is None:
        logger.info("no ecu_info.yaml in the OTA image, keep the current one")
        return

    try:
        _new_ecu_id = _load_ecu_id(_ecu_info)
    except Exception as e:
        logger.warning(
            f"ecu_info.yaml in the OTA image is invalid, keep the current one: {e!r}"
        )
        return

    try:
        _current_ecu_id = _load_ecu_id(current_ecu_info_fpath.read_text())
    except Exception as e:
        # NOTE: without a valid ecu_info.yaml, the default ecu_info is in use,
        #       there is nothing to keep.
        logger.info(f"no valid ecu_info.yaml currently in use: {e!r}")
        _current_ecu_id = None

    if _current_ecu_id is not None and _current_ecu_id != _new_ecu_id:
        logger.warning(
            f"ecu_id in the OTA image's ecu_info.yaml({_new_ecu_id}) differs from "
            f"the current one({_current_ecu_id}), keep the current ecu_info.yaml"
        )
        return

    logger.info("install ecu_info.yaml from the OTA image")
    write_str_to_file_atomic(dst_ota_dpath / ECU_INFO_FNAME, _ecu_info)
