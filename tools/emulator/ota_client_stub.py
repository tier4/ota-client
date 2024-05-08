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


from pathlib import Path
from threading import Thread, Timer

import log_setting
import otaclient_v2_pb2 as v2
from configs import config as cfg
from ecu import Ecu

logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OtaClientStub:
    def __init__(self, ecus: list, terminate=None):
        # check if all the names are unique
        names = [ecu._name for ecu in ecus]
        assert len(names) == len(set(names))
        # check if only one ecu is main
        mains = [ecu for ecu in ecus if ecu._is_main]
        assert len(mains) == 1

        self._ecus = ecus
        self._main_ecu = mains[0]
        self._terminate = terminate

    async def update(self, request: v2.UpdateRequest) -> v2.UpdateResponse:
        logger.info(f"{request=}")
        response = v2.UpdateResponse()

        for ecu in self._ecus:
            entry = OtaClientStub._find_request(request.ecu, ecu._name)
            if entry:
                logger.info(f"{ecu=}, {entry.version=}")
                response_ecu = response.ecu.add()
                ecu.update(response_ecu, entry.version)

        logger.info(f"{response=}")
        return response

    def rollback(self, request):
        logger.info(f"{request=}")
        response = v2.RollbackResponse()

        return response

    async def status(self, request: v2.StatusRequest) -> v2.StatusResponse:
        logger.info(f"{request=}")
        response = v2.StatusResponse()

        for ecu in self._ecus:
            response_ecu = response.ecu.add()
            ecu.status(response_ecu)
            response.available_ecu_ids.extend([ecu._name])

        logger.debug(f"{response=}")

        if self._sub_ecus_success_and_main_ecu_phase_persistent(response.ecu):
            self._main_ecu.change_to_success()
            for index, ecu in enumerate(self._ecus):
                self._ecus[index] = ecu.create()  # create new ecu instances
            self._terminate(self._main_ecu._time_to_restart)

        return response

    @staticmethod
    def _find_request(update_request, ecu_id):
        for request in update_request:
            if request.ecu_id == ecu_id:
                return request
        return None

    def _update(self, ecu, response):
        ecu.update(response)

    def _status(self, ecu, response):
        ecu.status(response)

    def _sub_ecus_success_and_main_ecu_phase_persistent(self, response_ecu):
        for ecu in response_ecu:
            if ecu.ecu_id == self._main_ecu._name:
                if (
                    ecu.status.status != v2.StatusOta.UPDATING
                    or ecu.status.progress.phase != v2.StatusProgressPhase.PERSISTENT
                ):
                    return False
            else:
                if ecu.status.status != v2.StatusOta.SUCCESS:
                    return False
        return True
