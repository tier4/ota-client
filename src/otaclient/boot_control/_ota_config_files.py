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

ota_core decides which files to install with load_ota_config_files_from_image,
    and the boot controller installs them for the next boot.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath
from typing import Iterable, NamedTuple

from otaclient.configs._ecu_info import load_ecu_info
from otaclient.configs._proxy_info import load_proxy_info
from otaclient.configs.cfg import cfg
from otaclient_common import replace_root
from otaclient_common._io import write_str_to_file_atomic
from otaclient_common.common import copytree_identical

logger = logging.getLogger(__name__)

ECU_INFO_FNAME = Path(cfg.ECU_INFO_FPATH).name
PROXY_INFO_FNAME = Path(cfg.PROXY_INFO_FPATH).name


class OTAConfigFiles(NamedTuple):
    """The contents of the OTA config files to install.

    A file that is None is not installed, the current one is kept for it.
    """

    ecu_info: str | None = None
    proxy_info: str | None = None

    def iter_files(self) -> Iterable[tuple[str, str]]:
        """Yield the file name and the content of each file to install."""
        if self.ecu_info is not None:
            yield ECU_INFO_FNAME, self.ecu_info
        if self.proxy_info is not None:
            yield PROXY_INFO_FNAME, self.proxy_info


def _is_persisted(fpath: PurePosixPath, persist_entries: Iterable[str]) -> bool:
    """Whether <fpath> is listed in the persist files, by itself or by one of its parents."""
    for _entry in persist_entries:
        _entry_path = PurePosixPath(os.path.normpath(_entry))
        if _entry_path == fpath or _entry_path in fpath.parents:
            return True
    return False


def _read_image_file(fpath: Path) -> str | None:
    # NOTE: not following the symlink, which is resolved against the running
    #       rootfs instead of the OTA image.
    if fpath.is_symlink():
        logger.warning(f"{fpath} in the OTA image is a symlink, keep the current one")
        return None
    if not fpath.is_file():
        logger.info(f"no {fpath.name} in the OTA image, keep the current one")
        return None
    try:
        return fpath.read_text()
    except UnicodeDecodeError as e:
        logger.warning(
            f"{fpath} in the OTA image is not UTF-8, keep the current one: {e!r}"
        )
        return None


def load_ota_config_files_from_image(
    image_ota_dpath: Path,
    *,
    persist_entries: Iterable[str],
    current_ecu_id: str,
) -> OTAConfigFiles:
    """Load the OTA config files to install from the OTA image.

    This must be called before the persist files handling, which replaces the
        persisted files in the OTA image with the active slot's ones.

    A file is not loaded, so that the current one is kept, when:
    1. it is listed in the persist files, by itself or by one of its parent folders,
    2. it is not shipped in the OTA image as a regular UTF-8 text file, or
    3. it is not valid.

    Neither file is loaded when the OTA image's ecu_info.yaml has an ecu_id different
        from <current_ecu_id>, as the OTA image is then not for this ECU, and the ecu_id
        of an ECU is not expected to be changed via OTA. This check is only done when
        the OTA image's ecu_info.yaml is loaded.

    Args:
        image_ota_dpath: the /boot/ota folder of the OTA image.
        persist_entries: the persist files of the OTA image.
        current_ecu_id: the ecu_id of the running otaclient.
    """
    if image_ota_dpath.is_symlink():
        logger.warning(
            f"{image_ota_dpath} in the OTA image is a symlink, "
            "keep the current OTA config files"
        )
        return OTAConfigFiles()

    _persist_entries = list(persist_entries)
    _loaded: dict[str, str | None] = {}
    for _fname in (ECU_INFO_FNAME, PROXY_INFO_FNAME):
        if _is_persisted(PurePosixPath(cfg.OTA_DPATH) / _fname, _persist_entries):
            logger.info(
                f"{_fname} is listed in the persist files, keep the current one"
            )
            _loaded[_fname] = None
        else:
            _loaded[_fname] = _read_image_file(image_ota_dpath / _fname)

    _ecu_info, _proxy_info = _loaded[ECU_INFO_FNAME], _loaded[PROXY_INFO_FNAME]
    if _ecu_info is not None:
        try:
            _new_ecu_id = load_ecu_info(_ecu_info).ecu_id
        except Exception as e:
            logger.warning(
                f"ecu_info.yaml in the OTA image is invalid, keep the current one: {e!r}"
            )
            _ecu_info = None
        else:
            if _new_ecu_id != current_ecu_id:
                logger.warning(
                    f"ecu_id in the OTA image's ecu_info.yaml({_new_ecu_id}) differs from "
                    f"the current one({current_ecu_id}), the OTA image is not for this ECU, "
                    "keep the current ecu_info.yaml and proxy_info.yaml"
                )
                return OTAConfigFiles()

    if _proxy_info is not None:
        try:
            load_proxy_info(_proxy_info)
        except Exception as e:
            logger.warning(
                f"proxy_info.yaml in the OTA image is invalid, keep the current one: {e!r}"
            )
            _proxy_info = None

    return OTAConfigFiles(ecu_info=_ecu_info, proxy_info=_proxy_info)


def install_ota_config_files(
    ota_config_files: OTAConfigFiles, *, dst_ota_dpath: Path
) -> None:
    """Install <ota_config_files> to <dst_ota_dpath>, a file that is None is left as it is."""
    _files = list(ota_config_files.iter_files())
    if not _files:
        logger.info(
            f"no OTA config files from the OTA image to install to {dst_ota_dpath}"
        )
        return

    dst_ota_dpath.mkdir(exist_ok=True, parents=True)
    for _fname, _content in _files:
        _dst = dst_ota_dpath / _fname
        logger.info(f"install {_fname} from the OTA image to {_dst}")
        # NOTE: replace the symlink at <_dst> instead of writing to its target.
        write_str_to_file_atomic(_dst, _content, follow_symlink=False)


def preserve_ota_dir_and_install_config_files(
    *,
    active_slot_mp: Path,
    standby_slot_mp: Path,
    ota_config_files: OTAConfigFiles,
) -> None:
    """Preserve /boot/ota to the standby slot, and install <ota_config_files> over it.

    This is for the boot controllers that have /boot/ota in each slot's rootfs.
    The standby slot's /boot/ota is made identical to the active slot's one, so that
        nothing from the OTA image's /boot/ota is left, and a symlink in it is
        replaced instead of written through.
    """
    _active_ota_dpath = Path(
        replace_root(cfg.OTA_DPATH, cfg.CANONICAL_ROOT, active_slot_mp)
    )
    _standby_ota_dpath = Path(
        replace_root(cfg.OTA_DPATH, cfg.CANONICAL_ROOT, standby_slot_mp)
    )
    if not _active_ota_dpath.is_dir():  # basically this should not happen
        logger.warning(
            f"{_active_ota_dpath} doesn't exist, skip preserve /boot/ota folder."
        )
        return

    copytree_identical(_active_ota_dpath, _standby_ota_dpath)
    install_ota_config_files(ota_config_files, dst_ota_dpath=_standby_ota_dpath)


def backup_ota_config_files(
    ota_dpath: Path, ota_config_files: OTAConfigFiles
) -> dict[str, bytes | None]:
    """Backup the files at <ota_dpath> that <ota_config_files> will replace.

    Returns:
        A dict maps the file name to the file content, None for a file that doesn't exist.
    """
    _res: dict[str, bytes | None] = {}
    for _fname, _ in ota_config_files.iter_files():
        _fpath = ota_dpath / _fname
        _res[_fname] = _fpath.read_bytes() if _fpath.is_file() else None
    return _res


def restore_ota_config_files(ota_dpath: Path, backup: dict[str, bytes | None]) -> None:
    """Restore the OTA config files at <ota_dpath> from <backup>, best effort."""
    for _fname, _content in backup.items():
        _fpath = ota_dpath / _fname
        try:
            if _content is None:
                _fpath.unlink(missing_ok=True)
            else:
                _tmp = _fpath.with_name(f".{_fname}.restore")
                with open(_tmp, "wb") as f:
                    f.write(_content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(_tmp, _fpath)
            logger.warning(f"restored {_fpath}")
        except Exception as e:
            logger.error(f"failed to restore {_fpath}: {e!r}")
