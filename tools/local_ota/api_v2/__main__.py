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
"""A simple tool to trigger an OTA locally."""


from __future__ import annotations

import argparse
import asyncio
import logging
import yaml
from pathlib import Path
from typing import List, Iterator

from pydantic import BaseModel, RootModel
from typing_extensions import Self

from otaclient_api.v2 import types
from otaclient_api.v2.api_caller import OTAClientCall, ECUNoResponse
from otaclient_common.typing import StrOrPath


logger = logging.getLogger(__name__)

DEFAULT_PORT = 50051


class UpdateRequest(BaseModel):
    ecu_id: str
    version: str
    url: str
    cookies: str


class UpdateRequestList(RootModel):
    root: List[UpdateRequest]

    def __iter__(self) -> Iterator[UpdateRequest]:
        return iter(self.root)

    @classmethod
    def load_update_request_yaml(cls, fpath: StrOrPath) -> Self:
        _raw = Path(fpath).read_text()
        _loaded = yaml.safe_load(_raw)
        assert isinstance(_loaded, list)

        return cls.model_validate(_loaded)


def generate_request(req_entries: UpdateRequestList) -> types.UpdateRequest:
    return types.UpdateRequest(
        ecu=(
            types.UpdateRequestEcu(
                ecu_id=entry.ecu_id,
                version=entry.version,
                url=entry.url,
                cookies=entry.cookies,
            )
            for entry in req_entries
        )
    )


async def main(
    host: str,
    port: int,
    *,
    req: types.UpdateRequest,
    timeout: int = 3,
) -> None:
    try:
        resp = await OTAClientCall.update_call(
            "not_used",
            host,
            port,
            request=req,
            timeout=timeout,
        )
        logger.info(f"update response: {resp}")
    except ECUNoResponse as e:
        _err_msg = f"ECU doesn't response to the {req}: {e}"
        logger.error(_err_msg)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="Calling ECU's update API, API version v2",
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
    parser.add_argument(
        "request_yaml",
        default="update_request.yaml",
        help="request to send, described in a yaml file",
    )

    args = parser.parse_args()

    req = generate_request(
        UpdateRequestList.load_update_request_yaml(args.request_yaml)
    )
    logger.info(f"loaded update request: {req}")
    asyncio.run(
        main(
            args.host,
            args.port,
            req=req,
        )
    )
