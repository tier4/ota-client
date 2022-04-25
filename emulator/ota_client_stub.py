import os
import sys
import time
from threading import Timer, Thread

from pathlib import Path
import otaclient_v2_pb2 as v2

from configs import config as cfg
import log_util

from ecu import Ecu

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


def export_and_exit():
    os.kill(os.getpid(), 9)


class OtaClientStub:
    def __init__(self, ecus: list):
        # check if all the names are unique
        names = [ecu._name for ecu in ecus]
        assert len(names) == len(set(names))
        # check if only one ecu is main
        mains = [ecu._is_main for ecu in ecus if ecu._is_main]
        assert len(mains) == 1

        self._ecus = ecus

    def host_addr(self):
        return "localhost"

    async def update(self, request: v2.UpdateRequest) -> v2.UpdateResponse:
        logger.info(f"{request=}")
        response = v2.UpdateResponse()

        for ecu in self._ecus:
            entry = OtaClientStub._find_request(request.ecu, ecu._name)
            if entry:
                logger.info(ecu)
                ecu.update(response)

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
            ecu.status(response)
            response.available_ecu_ids.extend([ecu._name])

        logger.info(f"{response=}")
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
