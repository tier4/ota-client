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


import asyncio
import yaml
from otaclient.app.ota_client_call import OtaClientCall
from otaclient.app.proto import wrapper
from . import _logutil

logger = _logutil.get_logger(__name__)


def load_external_update_request(request_yaml_file: str) -> wrapper.UpdateRequest:
    with open(request_yaml_file, "r") as f:
        try:
            request_yaml = yaml.safe_load(f)
            assert isinstance(request_yaml, list), "expect update request to be a list"
        except Exception as e:
            logger.exception(f"invalid update request yaml: {e!r}")
            raise

        logger.info(f"load external request: {request_yaml!r}")
        request = wrapper.UpdateRequest()
        for request_ecu in request_yaml:
            request.ecu.append(wrapper.UpdateRequestEcu(**request_ecu))
    return request


async def call_update(
    ecu_id: str,
    ecu_ip: str,
    ecu_port: int,
    *,
    request_file: str,
):
    logger.debug(f"request update on ecu(@{ecu_id}) at {ecu_ip}:{ecu_port}")
    update_request = load_external_update_request(request_file)

    try:
        update_response = await OtaClientCall.update_call(
            ecu_id, ecu_ip, ecu_port, request=update_request
        )
        logger.info(f"{update_response.export_pb()=}")
    except Exception as e:
        logger.exception(f"update request failed: {e!r}")
