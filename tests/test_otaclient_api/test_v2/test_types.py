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
from google.protobuf.duration_pb2 import Duration as _Duration

from otaclient_api.v2 import otaclient_v2_pb2 as v2
from otaclient_api.v2 import types as api_types
from tests.utils import compare_message


@pytest.mark.parametrize(
    "origin_msg, converted_msg, wrapper_type",
    (
        # StatusProgress: with protobuf Enum, int and protobuf Duration
        (
            v2.StatusProgress(
                phase=v2.REGULAR,
                total_regular_files=123456,
                elapsed_time_download=_Duration(seconds=1, nanos=5678),
            ),
            api_types.StatusProgress(
                phase=api_types.StatusProgressPhase.REGULAR,
                total_regular_files=123456,
                elapsed_time_download=api_types.Duration.from_nanoseconds(
                    1_000_005_678
                ),
            ),
            api_types.StatusProgress,
        ),
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
        # StatusResponse: multiple layer nested message, multiple protobuf message types
        (
            v2.StatusResponse(
                ecu=[
                    v2.StatusResponseEcu(
                        ecu_id="ecu_1",
                        status=v2.Status(
                            status=v2.UPDATING,
                            progress=v2.StatusProgress(
                                phase=v2.REGULAR,
                                total_regular_files=123456,
                                elapsed_time_copy=_Duration(seconds=1, nanos=56789),
                            ),
                        ),
                    ),
                    v2.StatusResponseEcu(
                        ecu_id="ecu_2",
                        status=v2.Status(
                            status=v2.UPDATING,
                            progress=v2.StatusProgress(
                                phase=v2.REGULAR,
                                total_regular_files=456789,
                                elapsed_time_copy=_Duration(seconds=1, nanos=12345),
                            ),
                        ),
                    ),
                ],
                available_ecu_ids=["ecu_1", "ecu_2"],
            ),
            api_types.StatusResponse(
                ecu=[
                    api_types.StatusResponseEcu(
                        ecu_id="ecu_1",
                        status=api_types.Status(
                            status=api_types.StatusOta.UPDATING,
                            progress=api_types.StatusProgress(
                                phase=api_types.StatusProgressPhase.REGULAR,
                                total_regular_files=123456,
                                elapsed_time_copy=api_types.Duration.from_nanoseconds(
                                    1_000_056_789
                                ),
                            ),
                        ),
                    ),
                    api_types.StatusResponseEcu(
                        ecu_id="ecu_2",
                        status=api_types.Status(
                            status=api_types.StatusOta.UPDATING,
                            progress=api_types.StatusProgress(
                                phase=api_types.StatusProgressPhase.REGULAR,
                                total_regular_files=456789,
                                elapsed_time_copy=api_types.Duration.from_nanoseconds(
                                    1_000_012_345
                                ),
                            ),
                        ),
                    ),
                ],
                available_ecu_ids=["ecu_1", "ecu_2"],
            ),
            api_types.StatusResponse,
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
        _protobuf_enum = v2.UPDATING
        _wrapped = api_types.StatusOta.UPDATING
        assert _protobuf_enum == _wrapped

    def test_assign_to_protobuf_message(self):
        """wrapper enum can be directly assigned in protobuf message."""
        left, r = v2.StatusProgress(phase=v2.REGULAR), v2.StatusProgress(
            phase=api_types.StatusProgressPhase.REGULAR.value,  # type: ignore
        )
        compare_message(left, r)

    def test_used_in_message_wrapper(self):
        """wrapper enum can be exported."""
        left, r = (
            v2.StatusProgress(phase=v2.REGULAR),
            api_types.StatusProgress(
                phase=api_types.StatusProgressPhase.REGULAR
            ).export_pb(),
        )
        compare_message(left, r)

    def test_converted_from_protobuf_enum(self):
        """wrapper enum can be converted from and to protobuf enum."""
        _protobuf_enum = v2.REGULAR
        _converted = api_types.StatusProgressPhase(_protobuf_enum)
        assert _protobuf_enum == _converted
        assert _converted == api_types.StatusProgressPhase.REGULAR
