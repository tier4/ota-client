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


from __future__ import annotations

from collections.abc import Iterable, Mapping

from otaclient_common.proto_wrapper import (
    Duration,
    EnumWrapper,
    MessageMapContainer,
    MessageWrapper,
    RepeatedCompositeContainer,
    RepeatedScalarContainer,
    ScalarMapContainer,
    calculate_slots,
)

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
        int_field: int | None = ...,
        double_field: float | None = ...,
        str_field: str | None = ...,
        duration_field: Duration | Mapping | None = ...,
        enum_field: SampleEnum | str | None = ...,
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
        repeated_scalar_field: Iterable[str] | None = ...,
        repeated_composite_field: Iterable[InnerMessage | Mapping] | None = ...,
        nested_msg: InnerMessage | Mapping | None = ...,
        mapping_scalar_field: Mapping[str, str] | None = ...,
        mapping_composite_field: Mapping[int, InnerMessage] | None = ...,
    ) -> None: ...
