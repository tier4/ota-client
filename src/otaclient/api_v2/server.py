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
"""Create and launch OTA API server."""


from __future__ import annotations

import asyncio
import multiprocessing as mp

from otaclient._types import OTAClientStatus
from otaclient.api_v2.servicer import OTAClientControlFlags
from otaclient.app.configs import server_cfg
from otaclient.configs.ecu_info import ecu_info


def app_server_main(
    *,
    status_report_queue: mp.Queue[OTAClientStatus],
    operation_queue: mp.Queue,
    otaclient_control_flags: OTAClientControlFlags,
):
    """OTA API server process main.

    NOTE that the imports within this function have side-effect, so we don't
        import them globally.
    """
    import grpc.aio as grpc_aio

    from otaclient.api_v2.servicer import OTAClientAPIServicer
    from otaclient_api.v2 import otaclient_v2_pb2_grpc as v2_grpc
    from otaclient_api.v2.api_stub import OtaClientServiceV2

    service_stub = OTAClientAPIServicer(
        status_report_queue=status_report_queue,
        operation_queue=operation_queue,
        otaclient_control_flags=otaclient_control_flags,
    )
    ota_client_service_v2 = OtaClientServiceV2(service_stub)

    server = grpc_aio.server()
    v2_grpc.add_OtaClientServiceServicer_to_server(
        server=server, servicer=ota_client_service_v2
    )
    server.add_insecure_port(f"{ecu_info.ip_addr}:{server_cfg.SERVER_PORT}")

    async def _main():
        await server.start()
        await server.wait_for_termination()

    asyncio.run(_main())
