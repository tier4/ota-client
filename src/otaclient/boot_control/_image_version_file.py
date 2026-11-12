# Copyright 2026 TIER IV, INC. All rights reserved.
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
"""The version file the image build writes into the rootfs.

Web.Auto writes `/usr/share/ota/version.json` into every OTA image it builds, for the
vehicle to identify the image it runs. Its `image_id` is the ID the image is registered
under in the Firmware Registry, the one a release names it by, so it is the source of
truth for the image ID otaclient reports: it says what runs, where otaclient's record
says what was requested.

The release name and ID are given when the image is released, after the build, so no
image carries them. They come from otaclient's record, and only while that record is
about the image that runs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from otaclient._types import VersionDetail
from otaclient.configs.cfg import cfg
from otaclient_common import _env, replace_root
from otaclient_common._typing import StrOrPath

from ._ota_status_control import OTAStatusFilesControl

logger = logging.getLogger(__name__)


def load_image_version_detail(*, root: StrOrPath | None = None) -> VersionDetail | None:
    """The image ID the image at `root` names in its version file, as a version detail
    without a release; None when the image has no version file or it names no image ID.

    `root` defaults to the running image: the host's root, also when otaclient runs as
    an app image, whose own /usr is not the host's.
    """
    if root is None:
        root = _env.get_dynamic_client_chroot_path() or cfg.CANONICAL_ROOT
    _fpath = Path(replace_root(cfg.IMAGE_VERSION_FILE_FPATH, cfg.CANONICAL_ROOT, root))
    try:
        _image_id = json.loads(_fpath.read_text())["image_id"]
    except FileNotFoundError:
        logger.info(f"the image carries no version file at {_fpath}")
        return None
    except Exception as e:
        logger.warning(f"cannot read the image ID from {_fpath}: {e!r}")
        return None

    if not isinstance(_image_id, str) or not _image_id:
        logger.warning(f"{_fpath} names no image ID: {_image_id!r}")
        return None
    return VersionDetail(image_id=_image_id)


def load_active_slot_version_detail(
    ota_status_control: OTAStatusFilesControl,
) -> VersionDetail | None:
    """The version detail of the running slot, its image ID from the running image.

    - The running image has a version file: its image ID, with the release otaclient
      recorded when the record names the same image, else with no release. A record
      of another image (the slot was written by someone else since, or is not the one
      otaclient installed into) would pin the release to an image that does not run.
    - It has none (an image built before the file was written): otaclient's record.
    """
    _recorded = ota_status_control.load_active_slot_version_detail()
    _from_image = load_image_version_detail()
    if _from_image is None:
        return _recorded

    if _recorded is None:
        logger.info(
            f"the running image is image {_from_image.image_id}; no release recorded"
        )
        return _from_image
    if _recorded.image_id == _from_image.image_id:
        return _recorded

    logger.warning(
        f"the running image is image {_from_image.image_id}, but the release recorded "
        f"for this slot ({_recorded.release_name}) is of image {_recorded.image_id}: "
        "reporting the running image without a release"
    )
    return _from_image
