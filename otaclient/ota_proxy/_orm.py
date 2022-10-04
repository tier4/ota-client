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
import functools
import sqlite3
from dataclasses import asdict, astuple, dataclass, fields
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Type,
    Generic,
    TypeVar,
    Union,
    cast,
    overload,
)

NULL_TYPE = cast(Type, type(None))
SQLITE_DATATYPES = Union[
    int,  # INTEGER
    str,  # TEXT
    float,  # REAL
    bytes,  # BLOB
    bool,  # INTEGER 0, 1
    NULL_TYPE,  # NULL, read-only datatype
]
FV = TypeVar("FV", bound=SQLITE_DATATYPES)  # field value type
TYPE_CHECKER = Callable[[Any], bool]


@dataclass
class ColumnDescriptor(Generic[FV]):
    type_checker: TYPE_CHECKER
    field_type: Type[FV]

    def __init__(
        self,
        field_type: Type[FV],
        *constrains: str,
        type_guard: Union[Tuple[Type, ...], TYPE_CHECKER, bool] = False,
        default: Optional[FV] = None,
    ) -> None:
        # type checker
        self._enable_type_check = False if type_guard is False else True
        # default to check over the specific field type
        self.type_checker = lambda x: isinstance(x, field_type)
        if isinstance(type_guard, tuple):  # check over a list of types
            self.type_checker = lambda x: isinstance(x, type_guard)
        elif callable(type_guard):  # custom type guard function
            self.type_checker = type_guard

        self.field_type = field_type
        self.constrains = " ".join(constrains)  # TODO: constrains validation
        self._default = self.field_type() if default is None else default
        super().__init__()

    @overload
    def __get__(self, obj: None, objtype: type) -> "ColumnDescriptor[FV]":
        """Descriptor accessed via class."""
        ...

    @overload
    def __get__(self, obj, objtype: type) -> FV:
        """Descriptor accessed via bound instance."""
        ...

    def __get__(self, obj, objtype=None) -> Union[FV, "ColumnDescriptor[FV]"]:
        if obj is not None:
            if isinstance(obj, type):
                return self  # bound inst is type, treated same as accessed via class
            return getattr(obj, self._private_name)  # access via instance
        return self  # access via class, return the descriptor

    def __set__(self, obj, value: Any) -> None:
        # handle dataclass's default value setting behavior and NULL type assignment
        if isinstance(value, type(self)) or self.field_type == NULL_TYPE:
            return setattr(obj, self._private_name, self._default)
        # handle normal value setting
        if self._enable_type_check and not self.type_checker(value):
            raise TypeError(f"type_guard: expect {self.field_type}, get {type(value)}")
        # apply type conversion before assign
        setattr(obj, self._private_name, self.field_type(value))  # type: ignore

    def __set_name__(self, owner: type, name: str):
        self.owner = owner
        self._field_name = name
        self._private_name = f"_{owner.__name__}_{name}"

    @property
    def name(self) -> str:
        return self._field_name

    def check_type(self, value: Any) -> bool:
        return self.type_checker(value)


@dataclass
class ORMBase(Generic[FV]):
    @classmethod
    def row_to_meta(cls, row: Union[sqlite3.Row, Dict[str, Any]]):
        parsed = {}
        for field in fields(cls):
            try:
                field_name = field.name
                parsed[field_name] = row[field_name]
            except (IndexError, KeyError):
                pass
        return cls(**parsed)

    @classmethod
    def get_create_table_stmt(cls, table_name: str) -> str:
        _col_descriptors: List[ColumnDescriptor] = [
            getattr(cls, field.name) for field in fields(cls)
        ]
        return (
            f"CREATE TABLE {table_name}("
            + ", ".join(
                [f"{col._field_name} {col.constrains}" for col in _col_descriptors]
            )
            + ")"
        )

    @classmethod
    def get_col(cls, name: str) -> Optional[ColumnDescriptor]:
        try:
            return getattr(cls, name)
        except AttributeError:
            return

    @classmethod
    def contains_field(cls, _input: Union[str, ColumnDescriptor]) -> bool:
        if isinstance(_input, ColumnDescriptor):
            return _input.owner.__name__ == cls.__name__
        return isinstance(getattr(cls, _input), ColumnDescriptor)

    @classmethod
    @functools.lru_cache
    def get_shape(cls) -> str:
        return ",".join(["?"] * len(fields(cls)))

    def to_tuple(self) -> Tuple[FV]:
        return astuple(self)

    def to_dict(self) -> Dict[str, FV]:
        return asdict(self)
