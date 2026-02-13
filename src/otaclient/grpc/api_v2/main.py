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
"""Main entry for OTA API v2 grpc server."""

from __future__ import annotations

import asyncio
import atexit
import logging
from concurrent.futures import ThreadPoolExecutor
from multiprocessing.queues import Queue as mp_Queue
from typing import Callable, NoReturn

from otaclient._types import (
    IPCRequest,
    IPCResponse,
    MultipleECUStatusFlags,
)
from otaclient._utils import SharedOTAClientStatusReader

logger = logging.getLogger(__name__)


def grpc_server_process(
    *,
    shm_reader_factory: Callable[[], SharedOTAClientStatusReader],
    op_queue: mp_Queue[IPCRequest],
    resp_queue: mp_Queue[IPCResponse],
    ecu_status_flags: MultipleECUStatusFlags,
) -> NoReturn:  # type: ignore
    from otaclient._logging import configure_logging

    configure_logging()
    logger.info("otaclient OTA API grpc server started")

    shm_reader = shm_reader_factory()
    atexit.register(shm_reader.atexit)

    async def _grpc_server_launcher():
        import grpc.aio

        from otaclient.configs.cfg import cfg, ecu_info
        from otaclient.grpc.api_v2.ecu_status import ECUStatusStorage
        from otaclient.grpc.api_v2.ecu_tracker import ECUTracker
        from otaclient.grpc.api_v2.servicer import OTAClientAPIServicer
        from otaclient_api.v2 import otaclient_v2_pb2_grpc as v2_grpc
        from otaclient_api.v2.api_stub import OtaClientServiceV2

        ecu_status_storage = ECUStatusStorage(ecu_status_flags=ecu_status_flags)
        ecu_tracker = ECUTracker(ecu_status_storage, shm_reader)
        ecu_tracker.start()

        thread_pool = ThreadPoolExecutor(
            thread_name_prefix="ota_api_server",
        )
        api_servicer = OTAClientAPIServicer(
            ecu_status_storage=ecu_status_storage,
            op_queue=op_queue,
            resp_queue=resp_queue,
            shm_reader=shm_reader,
            executor=thread_pool,
        )
        ota_client_service_v2 = OtaClientServiceV2(api_servicer)

        server = grpc.aio.server(migration_thread_pool=thread_pool)
        v2_grpc.add_OtaClientServiceServicer_to_server(
            server=server, servicer=ota_client_service_v2
        )
        _address_info = f"{ecu_info.ip_addr}:{cfg.OTA_API_SERVER_PORT}"
        server.add_insecure_port(_address_info)

        try:
            logger.info(f"launch grpc API server at {_address_info}")
            await server.start()
            logger.info("gRPC API server started successfully")
            await server.wait_for_termination()
        except Exception as e:
            logger.exception(f"gRPC server terminated with exception: {e}")
        finally:
            await server.stop(1)
            thread_pool.shutdown(wait=True)

    asyncio.run(_grpc_server_launcher())
