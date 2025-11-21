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
"""Implementation of mounting/umounting external cache."""

from __future__ import annotations

import logging

from ota_proxy.config import config
from otaclient_common import cmdhelper
from otaclient_common._typing import StrOrPath

logger = logging.getLogger(__name__)


def mount_external_cache(
    mnt_point: StrOrPath,
    is_nfs_cache: bool = False,
    *,
    cache_dev_fslabel: str = config.EXTERNAL_CACHE_DEV_FSLABEL,
) -> StrOrPath | None:
    logger.info(
        f"otaproxy will try to detect external cache dev and mount to {mnt_point}"
    )

    if is_nfs_cache:
        # NFS is pre-mounted externally, just verify it's ready
        if not cmdhelper.is_target_mounted(mnt_point, raise_exception=False):
            logger.warning(f"NFS cache not mounted at {mnt_point}")
            return None
        logger.info(f"NFS cache detected at {mnt_point}")
        return mnt_point

    _cache_dev = cmdhelper.get_dev_by_token(
        "LABEL",
        cache_dev_fslabel,
        raise_exception=False,
    )
    if not _cache_dev:
        logger.info("no cache dev is attached")
        return

    if len(_cache_dev) > 1:
        logger.warning(
            f"multiple external cache storage device found, use the first one: {_cache_dev[0]}"
        )
    _cache_dev = _cache_dev[0]
    logger.info(f"external cache dev detected at {_cache_dev}")

    try:
        cmdhelper.ensure_mount_point(mnt_point, ignore_error=True)
        cmdhelper.ensure_mount(
            target=_cache_dev,
            mnt_point=mnt_point,
            mount_func=cmdhelper.mount_ro,
            raise_exception=True,
            max_retry=3,
        )
        logger.info(
            f"successfully mount external cache dev {_cache_dev} on {mnt_point}"
        )
        return mnt_point
    except Exception as e:
        logger.warning(f"failed to mount external cache: {e!r}")


def umount_external_cache(mnt_point: StrOrPath) -> None:
    try:
        cmdhelper.ensure_umount(mnt_point, ignore_error=False)
    except Exception as e:
        logger.warning(f"failed to umount external cache {mnt_point=}: {e!r}")
