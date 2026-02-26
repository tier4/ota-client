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


from typing import Type

import pytest
from otaclient_pb2.v2 import otaclient_v2_pb2 as v2

from otaclient_api.v2 import _types as api_types
from tests.utils import compare_message


@pytest.mark.parametrize(
    "origin_msg, converted_msg, wrapper_type",
    (
        # UpdateRequest: with protobuf repeated composite field
        (
            v2.UpdateRequest(
                ecu=[
                    v2.UpdateRequestEcu(ecu_id="ecu_1"),
                    v2.UpdateRequestEcu(ecu_id="ecu_2"),
                ]
            ),
            api_types.UpdateRequest(
                ecu=[
                    api_types.UpdateRequestEcu(ecu_id="ecu_1"),
                    api_types.UpdateRequestEcu(ecu_id="ecu_2"),
                ]
            ),
            api_types.UpdateRequest,
        ),
        # UpdateRequest: with protobuf repeated composite field,
        (
            v2.UpdateRequest(
                ecu=[
                    v2.UpdateRequestEcu(ecu_id="ecu_1"),
                    v2.UpdateRequestEcu(ecu_id="ecu_2"),
                ]
            ),
            api_types.UpdateRequest(
                ecu=[
                    api_types.UpdateRequestEcu(ecu_id="ecu_1"),
                    api_types.UpdateRequestEcu(ecu_id="ecu_2"),
                ]
            ),
            api_types.UpdateRequest,
        ),
    ),
)
def test_convert_message(
    origin_msg,
    converted_msg: api_types.MessageWrapper,
    wrapper_type: Type[api_types.MessageWrapper],
):
    # ------ converting message ------ #
    _converted = wrapper_type.convert(origin_msg)
    _exported = _converted.export_pb()

    # ------ assertion ------ #
    # ensure the convertion is expected
    assert converted_msg == _converted
    # ensure that the exported version is the same as the original version
    compare_message(origin_msg, _exported)


class Test_enum_wrapper_cooperate:
    def test_direct_compare(self):
        """protobuf enum and wrapper enum can compare directly."""
        _protobuf_enums = [v2.UPDATING, v2.ROLLBACKING, v2.CLIENT_UPDATING]
        for _protobuf_enum in _protobuf_enums:
            _wrapped = api_types.StatusOta(_protobuf_enum)
            assert _protobuf_enum == _wrapped
