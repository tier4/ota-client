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


import pytest
from google.protobuf.duration_pb2 import Duration as _Duration
from typing import Type

from otaclient.app.proto import wrapper, v2
from otaclient.app.proto._common import _NORMAL_PYTHON_TYPES


def _compare_message(l, r):
    """
    NOTE: we don't directly compare two protobuf message by ==
          due to the behavior difference between empty Duration and
          unset Duration.
    """
    if (_proto_class := type(l)) is not type(r):
        raise TypeError(f"{type(l)=} != {type(r)=}")

    for _attrn in _proto_class.__slots__:
        _attrv_l, _attrv_r = getattr(l, _attrn), getattr(r, _attrn)
        # first check each corresponding attr has the same type,
        assert type(_attrv_l) == type(_attrv_r), f"compare failed on {_attrn=}"

        if isinstance(_attrv_l, _NORMAL_PYTHON_TYPES):
            assert _attrv_l == _attrv_r
        else:
            _compare_message(_attrv_l, _attrv_r)


@pytest.mark.parametrize(
    "origin_msg, converted_msg, wrapper_type",
    (
        # StatusProgress: with protobuf Enum, int and protobuf Duration
        (
            v2.StatusProgress(
                phase=v2.REGULAR,
                total_regular_files=123456,
                elapsed_time_download=_Duration(nanos=5678),
            ),
            wrapper.StatusProgress(
                phase=wrapper.StatusProgressPhase.REGULAR,
                total_regular_files=123456,
                elapsed_time_download=wrapper.DurationWrapper(5678),
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
        #   init wrapper with protobuf messages in repeated field.
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
                    v2.UpdateRequestEcu(ecu_id="ecu_2"),  # type: ignore
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
                                elapsed_time_copy=_Duration(nanos=56789),
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
                                elapsed_time_copy=_Duration(nanos=12345),
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
                                elapsed_time_copy=wrapper.DurationWrapper(56789),
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
                                elapsed_time_copy=wrapper.DurationWrapper(12345),
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
    converted_msg: wrapper.ProtobufConverter,
    wrapper_type: Type[wrapper.ProtobufConverter],
):
    # ------ converting message ------ #
    _converted = wrapper_type.convert(origin_msg)
    _exported = _converted.export_pb()

    # ------ assertion ------ #
    # ensure the convertion is expected
    # logger.error(f"{converted_msg=!s}, {_converted=!s}")
    assert converted_msg == _converted
    # ensure that the exported version is the same as the original version
    _compare_message(origin_msg, _exported)


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
        _compare_message(l, r)

    def test_used_in_message_wrapper(self):
        """wrapper enum can be exported."""
        l, r = (
            v2.StatusProgress(phase=v2.REGULAR),
            wrapper.StatusProgress(
                phase=wrapper.StatusProgressPhase.REGULAR
            ).export_pb(),
        )
        _compare_message(l, r)

    def test_converted_from_protobuf_enum(self):
        """wrapper enum can be converted from and to protobuf enum."""
        _protobuf_enum = v2.REGULAR
        _converted = wrapper.StatusProgressPhase(_protobuf_enum)
        assert _protobuf_enum == _converted
        assert _converted == wrapper.StatusProgressPhase.REGULAR
