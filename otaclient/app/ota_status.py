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


from .configs import config as cfg
from .proto import wrapper
from . import log_setting

logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class LiveOTAStatus:
    def __init__(self, ota_status: wrapper.StatusOta) -> None:
        self.live_ota_status = ota_status

    def get_ota_status(self) -> wrapper.StatusOta:
        return self.live_ota_status

    def set_ota_status(self, _status: wrapper.StatusOta):
        self.live_ota_status = _status

    def request_update(self) -> bool:
        return self.live_ota_status in [
            wrapper.StatusOta.INITIALIZED,
            wrapper.StatusOta.SUCCESS,
            wrapper.StatusOta.FAILURE,
            wrapper.StatusOta.ROLLBACK_FAILURE,
        ]

    def request_rollback(self) -> bool:
        return self.live_ota_status in [
            wrapper.StatusOta.SUCCESS,
            wrapper.StatusOta.ROLLBACK_FAILURE,
        ]
