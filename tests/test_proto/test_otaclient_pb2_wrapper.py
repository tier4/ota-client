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
from enum import IntEnum
from google.protobuf.message import Message as _Message
from google.protobuf.duration_pb2 import Duration as _Duration

from otaclient.app.proto import wrapper, v2
from otaclient.app.proto._common import TypeConverterRegister

_status_resp = v2.StatusResponse()
_status_progress = v2.StatusProgress()
_RepeatedScalarContainer = type(_status_resp.available_ecu_ids)
_RepeatedCompositeContainer = type(_status_resp.ecu)
del _status_resp, _status_progress


def _compare_message(l, r):
    if (_proto_class := type(l)) is not type(r):
        raise TypeError(f"{type(l)=} != {type(r)=}")
    for _attrn in _proto_class.__slots__:
        _attrv_l, _attrv_r = getattr(l, _attrn), getattr(r, _attrn)
        # first check each corresponding attr has the same type,
        # NOTE: for IntEnum types, we treat them as pure int without considering
        #       the type, but just comparing the value.
        if not isinstance(_attrv_l, IntEnum) or isinstance(_attrv_r, IntEnum):
            assert type(_attrv_l) == type(_attrv_r)

        # if the attr is nested message, we recursively call _compare_attrs
        if isinstance(_attrv_l, _Message):
            _compare_message(_attrv_l, _attrv_r)
        elif isinstance(_attrv_l, IntEnum) or isinstance(_attrv_r, IntEnum):
            assert _attrv_l == _attrv_r
        # if the attr is list of messages, call _compare_attrs on each message in list
        elif isinstance(_attrv_l, _RepeatedCompositeContainer):
            assert len(_attrv_l) == len(_attrv_r)
            for _msg_l, _msg_r in zip(_attrv_l, _attrv_r):
                _compare_message(_msg_l, _msg_r)
        # if the attr is list of normal types,
        elif isinstance(_attrv_r, _RepeatedScalarContainer):
            assert len(_attrv_l) == len(_attrv_r)
            assert set(_attrv_l) == set(_attrv_r)
        # for normal attr types, directly compare
        else:
            assert _attrv_l == _attrv_r


@pytest.mark.parametrize(
    "origin_msg, converted_msg",
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
        ),
    ),
)
def test_convert_message(origin_msg, converted_msg):
    # ------ converting message ------ #
    if not (_converter := TypeConverterRegister.get_converter(type(origin_msg))):
        raise NotImplementedError(
            f"converter for {type(origin_msg)} is not implemented"
        )
    _converted = _converter.convert(origin_msg)
    _exported = _converted.export_pb()

    # ------ assertion ------ #
    # ensure the convertion is expected
    assert converted_msg == _converted
    # ensure that the exported version is the same as the original version
    _compare_message(origin_msg, _exported)


class Test_enum_wrapper_cooperate:
    def test_direct_compare(self):
        _protobuf_enum = v2.UPDATING
        _wrapped = wrapper.StatusOta.UPDATING
        assert _protobuf_enum == _wrapped

    def test_assign_to_protobuf_message(self):
        l, r = v2.StatusProgress(phase=v2.REGULAR), v2.StatusProgress(
            phase=wrapper.StatusProgressPhase.REGULAR.value,
        )
        _compare_message(l, r)

    def test_used_in_message_wrapper(self):
        l, r = (
            v2.StatusProgress(phase=v2.REGULAR),
            wrapper.StatusProgress(
                phase=wrapper.StatusProgressPhase.REGULAR
            ).export_pb(),
        )
        _compare_message(l, r)

    def test_converted_from_protobuf_enum(self):
        _protobuf_enum = v2.REGULAR
        _converted = wrapper.StatusProgressPhase(_protobuf_enum)
        assert _protobuf_enum == _converted
        assert _converted == wrapper.StatusProgressPhase.REGULAR
