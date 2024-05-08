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


from typing import Any, Dict

import pytest
from google.protobuf.duration_pb2 import Duration as _pb2_Duration

from otaclient.app.proto import wrapper as proto_wrapper
from tests.utils import compare_message

from . import example_pb2 as pb2
from . import example_pb2_wrapper as wrapper


@pytest.mark.parametrize(
    "input_wrapper_inst, expected_dict",
    (
        # test case 1
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
        # test case 2
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
                "mapping_composite_field": proto_wrapper.MessageMapContainer(
                    key_type=int, value_converter=wrapper.InnerMessage
                ),
                "mapping_scalar_field": proto_wrapper.ScalarMapContainer(
                    key_type=str, value_type=str
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
        # test case 1
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
        # test case 2
        (
            pb2.OuterMessage(
                repeated_composite_field=[
                    pb2.InnerMessage(str_field="rc1"),
                    pb2.InnerMessage(str_field="rc2"),
                ],
                repeated_scalar_field=["rs1", "rs2"],
                nested_msg=pb2.InnerMessage(str_field="n1"),
                mapping_composite_field={
                    1: pb2.InnerMessage(str_field="m1"),
                    2: pb2.InnerMessage(str_field="m2"),
                },
                mapping_scalar_field={"m1": "m1", "m2": "m2"},
            ),
            wrapper.OuterMessage(
                repeated_composite_field=[
                    wrapper.InnerMessage(str_field="rc1"),
                    wrapper.InnerMessage(str_field="rc2"),
                ],
                repeated_scalar_field=["rs1", "rs2"],
                nested_msg=wrapper.InnerMessage(str_field="n1"),
                mapping_composite_field={
                    1: wrapper.InnerMessage(str_field="m1"),
                    2: wrapper.InnerMessage(str_field="m2"),
                },
                mapping_scalar_field={"m1": "m1", "m2": "m2"},
            ),
        ),
    ),
)
def test_convert_export(origin_msg, converted_msg: proto_wrapper.MessageWrapper):
    _converted = type(converted_msg).convert(origin_msg)
    assert _converted == converted_msg
    compare_message(origin_msg, _converted.export_pb())
