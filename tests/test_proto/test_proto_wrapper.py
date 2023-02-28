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
from google.protobuf.duration_pb2 import Duration as _pb2_Duration
from typing import Any, Dict
from tests.utils import compare_message

from otaclient.app.proto import wrapper as proto_wrapper
from . import example_pb2_wrapper as wrapper
from . import example_pb2 as pb2


@pytest.mark.parametrize(
    "input_wrapper_inst, expected_dict",
    (
        # test case1:
        (
            wrapper.InnerMessage(),
            {
                "duration_field": proto_wrapper.Duration(),
                "enum_field": wrapper.SampleEnum.VALUE_0,
                "double_field": 0.0,
                "int_field": 0,
                "str_field": "",
            },
        ),
        # test case2:
        (
            wrapper.OuterMessage(),
            {
                "nested_msg": wrapper.InnerMessage(),
                "repeated_composite_field": proto_wrapper.RepeatedCompositeContainer(
                    converter_type=wrapper.InnerMessage
                ),
                "repeated_scalar_field": proto_wrapper.RepeatedScalarContainer(
                    element_type=str
                ),
            },
        ),
    ),
)
def test_default_value_behavior(
    input_wrapper_inst: proto_wrapper.MessageWrapper, expected_dict: Dict[str, Any]
):
    for _field_name in input_wrapper_inst._fields:
        _value = getattr(input_wrapper_inst, _field_name)
        if isinstance(_value, proto_wrapper.MessageWrapper):
            compare_message(_value, expected_dict[_field_name])
        else:
            assert _value == expected_dict[_field_name]


@pytest.mark.parametrize(
    "origin_msg, converted_msg",
    (
        (
            pb2.InnerMessage(
                duration_field=_pb2_Duration(nanos=456, seconds=1),
                int_field=34567,
                double_field=34.567,
                str_field="34567",
                enum_field=pb2.VALUE_1,
            ),
            wrapper.InnerMessage(
                duration_field=proto_wrapper.Duration(nanos=456, seconds=1),
                int_field=34567,
                double_field=34.567,
                str_field="34567",
                enum_field=wrapper.SampleEnum.VALUE_1,
            ),
        ),
    ),
)
def test_convert_export(origin_msg, converted_msg: proto_wrapper.MessageWrapper):
    _converted = type(converted_msg).convert(origin_msg)
    assert _converted == converted_msg
    compare_message(origin_msg, _converted.export_pb())
