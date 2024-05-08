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


from typing import Iterable as _Iterable
from typing import Mapping as _Mapping
from typing import Optional as _Optional
from typing import Union as _Union

from otaclient.app.proto.wrapper import (Duration, EnumWrapper,
                                         MessageMapContainer, MessageWrapper,
                                         RepeatedCompositeContainer,
                                         RepeatedScalarContainer,
                                         ScalarMapContainer, calculate_slots)

from . import example_pb2 as _pb2


class SampleEnum(EnumWrapper):
    VALUE_0 = _pb2.VALUE_0
    VALUE_1 = _pb2.VALUE_1
    VALUE_2 = _pb2.VALUE_2


class InnerMessage(MessageWrapper[_pb2.InnerMessage]):
    __slots__ = calculate_slots(_pb2.InnerMessage)
    duration_field: Duration
    enum_field: SampleEnum
    double_field: float
    int_field: int
    str_field: str

    def __init__(
        self,
        int_field: _Optional[int] = ...,
        double_field: _Optional[float] = ...,
        str_field: _Optional[str] = ...,
        duration_field: _Optional[_Union[Duration, _Mapping]] = ...,
        enum_field: _Optional[_Union[SampleEnum, str]] = ...,
    ) -> None: ...


class OuterMessage(MessageWrapper[_pb2.OuterMessage]):
    __slots__ = calculate_slots(_pb2.OuterMessage)
    mapping_composite_field: MessageMapContainer[int, InnerMessage]
    mapping_scalar_field: ScalarMapContainer[str, str]
    nested_msg: InnerMessage
    repeated_composite_field: RepeatedCompositeContainer[InnerMessage]
    repeated_scalar_field: RepeatedScalarContainer[str]

    def __init__(
        self,
        repeated_scalar_field: _Optional[_Iterable[str]] = ...,
        repeated_composite_field: _Optional[
            _Iterable[_Union[InnerMessage, _Mapping]]
        ] = ...,
        nested_msg: _Optional[_Union[InnerMessage, _Mapping]] = ...,
        mapping_scalar_field: _Optional[_Mapping[str, str]] = ...,
        mapping_composite_field: _Optional[_Mapping[int, InnerMessage]] = ...,
    ) -> None: ...
