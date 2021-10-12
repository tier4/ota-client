from ota_client_stub import OtaClientStub
from ota_client_service import (
    OtaClientServiceV2,
    service_start,
    service_wait_for_termination,
)
import otaclient_v2_pb2_grpc as v2_grpc

import configs as cfg
import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


if __name__ == "__main__":
    ota_client_stub = OtaClientStub()
    ota_client_service_v2 = OtaClientServiceV2(ota_client_stub)

    server = service_start(
        "localhost:50051",
        [
            {"grpc": v2_grpc, "instance": ota_client_service_v2},
        ],
    )

    service_wait_for_termination(server)
