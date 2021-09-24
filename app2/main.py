from logging import getLogger

import ota_client_stub
import ota_client_service
import configs as cfg

logger = getLogger(__name__)
logger.setLevel(cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL))

if __name__ == "__main__":
    ota_client_stub = OtaClientStub()
    ota_client_service = OtaClientService(ota_client_stub)
    ota_client_service.service_start(50051)
    ota_client_service.service_wait_for_termination()
