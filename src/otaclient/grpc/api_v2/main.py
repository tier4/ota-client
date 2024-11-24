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
import multiprocessing.synchronize as mp_sync
from multiprocessing.queues import Queue as mp_Queue
from typing import Callable, NoReturn

from otaclient._types import IPCRequest, IPCResponse
from otaclient._utils import SharedOTAClientStatusReader

logger = logging.getLogger(__name__)


def grpc_server_process(
    shm_reader_factory: Callable[[], SharedOTAClientStatusReader],
    control_flag: mp_sync.Event,
    op_queue: mp_Queue[IPCRequest | IPCResponse],
    all_ecus_succeeded: mp_sync.Event,
    any_requires_network: mp_sync.Event,
) -> NoReturn:  # type: ignore
    from otaclient._logging import configure_logging

    configure_logging()

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

        ecu_status_storage = ECUStatusStorage(
            all_ecus_succeeded=all_ecus_succeeded,
            any_requires_network=any_requires_network,
        )
        ecu_tracker = ECUTracker(ecu_status_storage, shm_reader)
        ecu_tracker.start()

        api_servicer = OTAClientAPIServicer(
            ecu_status_storage,
            op_queue,
            control_flag=control_flag,
        )
        ota_client_service_v2 = OtaClientServiceV2(api_servicer)

        server = grpc.aio.server()
        v2_grpc.add_OtaClientServiceServicer_to_server(
            server=server, servicer=ota_client_service_v2
        )
        server.add_insecure_port(f"{ecu_info.ip_addr}:{cfg.OTA_API_SERVER_PORT}")

        await server.start()
        try:
            await server.wait_for_termination()
        finally:
            await server.stop(1)

    asyncio.run(_grpc_server_launcher())
