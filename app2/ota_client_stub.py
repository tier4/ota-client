from logging import getLogger

from ota_client import OtaClient
from ota_client_call import OtaClientCall
from ecu_info import EcuInfo
import configs as cfg

logger = getLogger(__name__)
logger.setLevel(cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL))


class OtaClientStub:
    def __init__(self):
        self._ota_client = OtaClient()
        self._ecu_info = EcuInfo()
        self._ota_client_call = OtaClientCall("50051")

    def update(self, request):
        response = []

        # secondary ecus
        secondary_ecus = self._ecu_info.get_secondary_ecus()
        for secondary in secondary_ecus:
            entry = OtaClientStub._find_request(request.update_request_ecu, secondary)
            if entry:
                r = self._ota_client_call.update(request, secondary["ip_addr"])
                response.append(r)

        # my ecu
        ecu_id = self._ecu_info.get_ecu_id()  # my ecu id
        entry = OtaClientStub._find_request(request.update_request_ecu, ecu_id)
        if entry:
            ret = 0  # FIXME
            try:
                self._ota_client.update(entry.version, entry.url, entry.cookies)
            except:
                ret = 1  # FIXME
            finally:
                response.append({"ecu_id": entry.ecu_id, "result": ret})

        return response

    def rollback(self, request):
        response = []

        # secondary ecus
        secondary_ecus = self._ecu_info.get_secondary_ecus()
        for secondary in secondary_ecus:
            entry = OtaClientStub._find_request(request.rollback_request_ecu, secondary)
            if entry:
                r = self._ota_client_call.rollback(request, secondary["ip_addr"])
                response.append(r)

        # my ecu
        ecu_id = self._ecu_info.get_ecu_id()  # my ecu id
        entry = OtaClientStub._find_request(request.rollback_request_ecu, ecu_id)
        if entry:
            r = self._ota_client.rollback()
            response.append(r)
        return response

    def status(self, request):
        response = []

        # secondary ecus
        secondary_ecus = self._ecu_info.get_secondary_ecus()
        for secondary in secondary_ecus:
            r = self._ota_client_call.status(request, secondary["ip_addr"])
            response.append(r)

        # my ecu
        r = self._ota_client.status()
        response.append(r)
        return response

    @staticmethod
    def _find_request(update_request, ecu_id):
        for request in update_request:
            if request.ecu_id == ecu_id:
                return request
        return None
