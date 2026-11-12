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

Only schema version 1 is read: the schema is Web.Auto's, and an otaclient older than
the image cannot know what a later version means by `image_id`. Any other version, like
a file that names no valid image ID, leaves the record to answer, as before the file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from pathlib import Path

from otaclient._types import VersionDetail
from otaclient.configs.cfg import cfg
from otaclient_common import _env, replace_root
from otaclient_common._typing import StrOrPath

from ._ota_status_control import OTAStatusFilesControl

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_VERSION_FILE_SIZE = 1024**2
"""Generous: a real one is about 10 KiB, most of it one commit per git repository."""
IMAGE_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,256}")
"""What an image ID is let through as: it goes into the uploaded logs and the status
API as it is, so nothing that would need escaping there."""


def _read_version_file(fpath: Path) -> bytes:
    """The file's bytes, if it is a regular file of a sane size.

    Not through a symlink: the writer never puts one there, and in an app image an
    absolute one would resolve in the app image's root, not the host's. Opened without
    blocking, so that a FIFO there cannot hold otaclient's startup.
    """
    _fd = os.open(fpath, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with open(_fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise ValueError("not a regular file")
        _data = f.read(MAX_VERSION_FILE_SIZE + 1)
    if len(_data) > MAX_VERSION_FILE_SIZE:
        raise ValueError(f"larger than {MAX_VERSION_FILE_SIZE} bytes")
    return _data


def load_image_version_detail(*, root: StrOrPath | None = None) -> VersionDetail | None:
    """The image ID the image at `root` names in its version file, as a version detail
    without a release; None when the image has no version file, one of another schema
    version, or one that names no valid image ID.

    `root` defaults to the running image: the host's root, also when otaclient runs as
    an app image, whose own /usr is not the host's.
    """
    if root is None:
        root = _env.get_dynamic_client_chroot_path() or cfg.CANONICAL_ROOT
    _fpath = Path(replace_root(cfg.IMAGE_VERSION_FILE_FPATH, cfg.CANONICAL_ROOT, root))
    try:
        _doc = json.loads(_read_version_file(_fpath))
    except FileNotFoundError:
        logger.info(f"the image carries no version file at {_fpath}")
        return None
    except Exception as e:
        # the type and message only: a repr may carry the whole file
        logger.warning(f"cannot read {_fpath}: {type(e).__name__}: {e}")
        return None

    _schema = _doc.get("schema_version") if isinstance(_doc, dict) else None
    if _schema != SCHEMA_VERSION:
        logger.warning(
            f"{_fpath} is not a version file of schema {SCHEMA_VERSION}: "
            f"schema_version={_schema!r:.32}"
        )
        return None
    _image_id = _doc.get("image_id")
    if not isinstance(_image_id, str) or not IMAGE_ID_RE.fullmatch(_image_id):
        logger.warning(f"{_fpath} names no valid image ID: {_image_id!r:.80}")
        return None
    return VersionDetail(image_id=_image_id)


def resolve_active_slot_version_detail(
    ota_status_control: OTAStatusFilesControl,
) -> VersionDetail | None:
    """The version detail of the running slot, its image ID from the running image.

    - The running image has a version file: its image ID, with the release otaclient
      recorded when the record names the same image, else with no release. A record
      of another image (the slot was written by someone else since, or is not the one
      otaclient installed into) would pin the release to an image that does not run.
    - It has none, or one that is not read (see the module): otaclient's record.

    The version (`firmware_version`) is not resolved here and stays the recorded one,
    as the file carries none. A record of another image therefore still shows its
    version there, and the upper layer, which takes a missing release name from that
    version, keeps showing it; the image ID is what tells the two apart.
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
