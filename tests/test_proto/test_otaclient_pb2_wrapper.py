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

from otaclient.app.proto import v2, wrapper
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
            wrapper.StatusProgress(
                phase=wrapper.StatusProgressPhase.REGULAR,
                total_regular_files=123456,
                elapsed_time_download=wrapper.Duration.from_nanoseconds(1_000_005_678),
            ),
            wrapper.StatusProgress,
        ),
        # UpdateRequest: with protobuf repeated composite field
        (
            v2.UpdateRequest(
                ecu=[
                    v2.UpdateRequestEcu(ecu_id="ecu_1"),
                    v2.UpdateRequestEcu(ecu_id="ecu_2"),
                ]
            ),
            wrapper.UpdateRequest(
                ecu=[
                    wrapper.UpdateRequestEcu(ecu_id="ecu_1"),
                    wrapper.UpdateRequestEcu(ecu_id="ecu_2"),
                ]
            ),
            wrapper.UpdateRequest,
        ),
        # UpdateRequest: with protobuf repeated composite field,
        (
            v2.UpdateRequest(
                ecu=[
                    v2.UpdateRequestEcu(ecu_id="ecu_1"),
                    v2.UpdateRequestEcu(ecu_id="ecu_2"),
                ]
            ),
            wrapper.UpdateRequest(
                ecu=[
                    wrapper.UpdateRequestEcu(ecu_id="ecu_1"),
                    wrapper.UpdateRequestEcu(ecu_id="ecu_2"),
                ]
            ),
            wrapper.UpdateRequest,
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
            wrapper.StatusResponse(
                ecu=[
                    wrapper.StatusResponseEcu(
                        ecu_id="ecu_1",
                        status=wrapper.Status(
                            status=wrapper.StatusOta.UPDATING,
                            progress=wrapper.StatusProgress(
                                phase=wrapper.StatusProgressPhase.REGULAR,
                                total_regular_files=123456,
                                elapsed_time_copy=wrapper.Duration.from_nanoseconds(
                                    1_000_056_789
                                ),
                            ),
                        ),
                    ),
                    wrapper.StatusResponseEcu(
                        ecu_id="ecu_2",
                        status=wrapper.Status(
                            status=wrapper.StatusOta.UPDATING,
                            progress=wrapper.StatusProgress(
                                phase=wrapper.StatusProgressPhase.REGULAR,
                                total_regular_files=456789,
                                elapsed_time_copy=wrapper.Duration.from_nanoseconds(
                                    1_000_012_345
                                ),
                            ),
                        ),
                    ),
                ],
                available_ecu_ids=["ecu_1", "ecu_2"],
            ),
            wrapper.StatusResponse,
        ),
    ),
)
def test_convert_message(
    origin_msg,
    converted_msg: wrapper.MessageWrapper,
    wrapper_type: Type[wrapper.MessageWrapper],
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
        _wrapped = wrapper.StatusOta.UPDATING
        assert _protobuf_enum == _wrapped

    def test_assign_to_protobuf_message(self):
        """wrapper enum can be directly assigned in protobuf message."""
        l, r = v2.StatusProgress(phase=v2.REGULAR), v2.StatusProgress(
            phase=wrapper.StatusProgressPhase.REGULAR.value,
        )
        compare_message(l, r)

    def test_used_in_message_wrapper(self):
        """wrapper enum can be exported."""
        l, r = (
            v2.StatusProgress(phase=v2.REGULAR),
            wrapper.StatusProgress(
                phase=wrapper.StatusProgressPhase.REGULAR
            ).export_pb(),
        )
        compare_message(l, r)

    def test_converted_from_protobuf_enum(self):
        """wrapper enum can be converted from and to protobuf enum."""
        _protobuf_enum = v2.REGULAR
        _converted = wrapper.StatusProgressPhase(_protobuf_enum)
        assert _protobuf_enum == _converted
        assert _converted == wrapper.StatusProgressPhase.REGULAR
