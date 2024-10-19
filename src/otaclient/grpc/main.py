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
"""OTA grpc server launcher.

NOTE: currently we only support OTA service API v2.
"""


from __future__ import annotations

import asyncio
import logging
import multiprocessing.synchronize as mp_sync
import signal
from multiprocessing.queues import Queue as mp_Queue
from typing import NoReturn

from otaclient.app.configs import ecu_info, server_cfg
from otaclient.log_setting import configure_logging

logger = logging.getLogger(__name__)


def _subp_signterm_handler(signame, frame) -> NoReturn:
    raise KeyboardInterrupt("receives SIGTERM, exits ...")


def apiv2_server_main(
    *,
    status_report_queue: mp_Queue,
    operation_push_queue: mp_Queue,
    operation_ack_queue: mp_Queue,
    any_requires_network: mp_sync.Event,
    no_child_ecus_in_update: mp_sync.Event,
    global_shutdown_flag: mp_sync.Event,
    all_ecus_succeeded: mp_sync.Event,
):  # pragma: no cover
    """OTA API server process main.

    NOTE that the imports within this function have side-effect, so we don't
        import them globally.
    """
    import grpc.aio as grpc_aio

    import otaclient
    from otaclient.grpc.api_v2.ecu_status import ECUStatusStorage, ECUTracker
    from otaclient.grpc.api_v2.servicer import OTAClientAPIServicer as APIv2Servicer
    from otaclient_api.v2 import otaclient_v2_pb2_grpc as v2_grpc

    # logging needed to be configured again at new process
    configure_logging()

    global _main_global_shutdown_flag
    _main_global_shutdown_flag = global_shutdown_flag

    otaclient._global_shutdown_flag = global_shutdown_flag
    # NOTE: spawn will not let the child process inherits the signal handler
    signal.signal(signal.SIGTERM, _subp_signterm_handler)

    async def _main():
        server = grpc_aio.server()
        server.add_insecure_port(f"{ecu_info.ip_addr}:{server_cfg.SERVER_PORT}")

        ecu_status_storage = ECUStatusStorage(
            any_requires_network=any_requires_network,
            no_child_ecus_in_update=no_child_ecus_in_update,
            all_ecus_succeeded=all_ecus_succeeded,
        )
        ecu_tracker = ECUTracker(
            ecu_status_storage=ecu_status_storage,
            status_report_queue=status_report_queue,
        )
        ecu_tracker.start_tracking()

        # mount API v2 servicer
        v2_grpc.add_OtaClientServiceServicer_to_server(
            server=server,
            servicer=APIv2Servicer(
                ecu_status_storage=ecu_status_storage,
                operation_push_queue=operation_push_queue,
                operation_ack_queue=operation_ack_queue,
            ),
        )

        logger.info("OTA API server started")
        await server.start()
        try:
            await server.wait_for_termination()
        finally:
            await server.stop(1)

    asyncio.run(_main())
