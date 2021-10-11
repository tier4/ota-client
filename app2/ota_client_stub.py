import asyncio
from typing import Awaitable

import configs as cfg
from ota_client import OtaClient, OtaClientFailureType
from ota_client_call import OtaClientCall
from ecu_info import EcuInfo

from logging import getLogger


logger = getLogger(__name__)
logger.setLevel(cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL))


class OtaClientStub:
    def __init__(self):
        self._ota_client = OtaClient()
        self._ecu_info = EcuInfo()
        self._ota_client_call = OtaClientCall("50051")

    async def _async_update(self, request):
        # secondary ecus
        tasks, secondary_ecus = [], self._ecu_info.get_secondary_ecus()
        for secondary in secondary_ecus:
            entry = OtaClientStub._find_request(request.ecu, secondary)
            if entry:
                tasks.append(self._ota_client_call.update(request, secondary["ip_addr"]))

        # dispatch sub-ecu updates async
        sub_ecu_update_aws: Awaitable = asyncio.gather(*tasks)

        # my ecu
        # start main ecu update process (blocking)
        ecu_id = self._ecu_info.get_ecu_id()  # my ecu id
        entry = OtaClientStub._find_request(request.ecu, ecu_id)
        if entry:
            result = self._ota_client.update(entry.version, entry.url, entry.cookies)
            main_ecu_update_result = {"ecu_id": entry.ecu_id, "result": result.value}

        response: list = await sub_ecu_update_aws
        response.append(main_ecu_update_result)

        return response

    def update(self, request):
        asyncio.run(self._async_update(request))

    def rollback(self, request):
        response = []

        # secondary ecus
        secondary_ecus = self._ecu_info.get_secondary_ecus()
        for secondary in secondary_ecus:
            entry = OtaClientStub._find_request(request.ecu, secondary)
            if entry:
                r = self._ota_client_call.rollback(request, secondary["ip_addr"])
                response.append(r)

        # my ecu
        ecu_id = self._ecu_info.get_ecu_id()  # my ecu id
        entry = OtaClientStub._find_request(request.ecu, ecu_id)
        if entry:
            result = self._ota_client.rollback()
            response.append({"ecu_id": entry.ecu_id, "result": result.value})

        return response

    def status(self, request):
        response = []

        # secondary ecus
        secondary_ecus = self._ecu_info.get_secondary_ecus()
        for secondary in secondary_ecus:
            r = self._ota_client_call.status(request, secondary["ip_addr"])
            response.append(r)

        # my ecu
        ecu_id = self._ecu_info.get_ecu_id()  # my ecu id
        result, status = self._ota_client.status()
        response.append({"ecu_id": ecu_id, "result": result.value, "status": status})

        return response

    @staticmethod
    def _find_request(update_request, ecu_id):
        for request in update_request:
            if request.ecu_id == ecu_id:
                return request
        return None
