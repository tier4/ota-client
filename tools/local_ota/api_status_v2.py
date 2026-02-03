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
"""A simple tool to query OTA status locally, for API version 2."""


from __future__ import annotations

import argparse
import asyncio
import logging

from otaclient_api.v2 import _types
from otaclient_api.v2.api_caller import ECUNoResponse, OTAClientCall

logger = logging.getLogger(__name__)

DEFAULT_PORT = 50051


async def main(
    host: str,
    port: int,
    *,
    req: _types.StatusRequest,
    timeout: int = 3,
) -> None:
    try:
        resp = await OTAClientCall.status_call(
            "not_used",
            host,
            port,
            request=req,
            timeout=timeout,
        )
        logger.info(f"status response: {resp}")
    except ECUNoResponse as e:
        _err_msg = f"ECU doesn't response to the request on-time({timeout=}): {e}"
        logger.error(_err_msg)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="Calling ECU's status API, API version v2",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "-i",
        "--host",
        help="ECU listen IP address",
        required=True,
    )
    parser.add_argument(
        "-p",
        "--port",
        help="ECU listen port",
        type=int,
        default=DEFAULT_PORT,
    )

    args = parser.parse_args()

    status_request = _types.StatusRequest()
    asyncio.run(
        main(
            args.host,
            args.port,
            req=status_request,
        )
    )
