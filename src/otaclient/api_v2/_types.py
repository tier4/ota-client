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
"""Helpers for converting otaclient internal used types into api_v2 types."""


from __future__ import annotations

import time

from _otaclient_version import __version__
from otaclient._types import OTAClientStatus, OTAStatus, UpdateTiming
from otaclient_api.v2 import types as api_types
from otaclient_common.proto_wrapper import Duration


def _calculate_elapsed_time(
    _in: UpdateTiming, _res: api_types.UpdateStatus
) -> api_types.UpdateStatus:
    _now = int(time.time())
    if (update_started_timestamp := _in.update_start_timestamp) <= 0:
        # update not yet started
        return _res
    _res.update_start_timestamp = update_started_timestamp

    if (delta_calculated_started_timestamp := _in.delta_generate_start_timestamp) <= 0:
        # delta calculation not yet started
        return _res

    if (downloaded_started_timestamp := _in.download_start_timestamp) <= 0:
        # download not yet started
        _res.delta_generating_elapsed_time = Duration(
            seconds=min(1, _now - delta_calculated_started_timestamp)
        )
        return _res
    _res.delta_generating_elapsed_time = Duration(
        seconds=min(
            1, delta_calculated_started_timestamp - downloaded_started_timestamp
        )
    )

    if (update_apply_started_timestamp := _in.update_apply_start_timestamp) <= 0:
        # apply update not yet started
        _res.downloading_elapsed_time = Duration(
            seconds=min(1, _now - downloaded_started_timestamp)
        )
        return _res
    _res.downloading_elapsed_time = Duration(
        seconds=min(1, update_apply_started_timestamp - downloaded_started_timestamp)
    )

    if (post_update_started_timestamp := _in.post_update_start_timestamp) <= 0:
        # post update not yet started
        _res.downloading_elapsed_time = Duration(
            seconds=min(1, _now - update_apply_started_timestamp)
        )
        return _res
    _res.update_applying_elapsed_time = Duration(
        seconds=min(1, post_update_started_timestamp - _in.update_apply_start_timestamp)
    )
    return _res


def convert_status(_in: OTAClientStatus) -> api_types.StatusResponseEcuV2:
    base_res = api_types.StatusResponseEcuV2(
        ecu_id=_in.ecu_id,
        failure_type=_in.failure_type,
        failure_reason=_in.failure_reason,
        ota_status=_in.ota_status,
        otaclient_version=__version__,
        firmware_version=_in.firmware_version,
    )

    if _in.ota_status != OTAStatus.UPDATING or not (
        _in.update_meta
        and _in.update_phase
        and _in.update_progress
        and _in.update_timing
    ):
        return base_res

    # for UPDATING OTAStatus, convert api_types' update_status attr
    update_status = api_types.UpdateStatus()

    _now = int(time.time())
    update_started_timestamp = _in.update_timing.update_start_timestamp

    # update_progress
    _update_progress = _in.update_progress
    update_status.downloaded_bytes = _update_progress.downloaded_bytes
    update_status.downloaded_files_num = _update_progress.downloaded_files_num
    update_status.downloaded_files_size = _update_progress.downloaded_files_size

    # update_meta
    _update_meta = _in.update_meta
    update_status.total_download_files_num = _update_meta.total_download_files_num
    update_status.total_download_files_size = _update_meta.total_download_files_size
    update_status.total_elapsed_time = Duration(seconds=_now - update_started_timestamp)
    update_status.update_firmware_version = _update_meta.update_firmware_version
    update_status.total_files_num = _update_meta.image_file_entries
    update_status.total_files_size_uncompressed = _update_meta.image_size_uncompressed

    # update_timing
    _calculate_elapsed_time(_in.update_timing, update_status)

    base_res.update_status = update_status
    return base_res
