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
from dataclasses import asdict, astuple, dataclass, fields
from io import StringIO
from typing import (
    TYPE_CHECKING,
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

if TYPE_CHECKING:
    import sqlite3

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


class ColumnDescriptor(Generic[FV]):
    """ColumnDescriptor represents a column in a sqlite3 table,
    implemented the python descriptor protocol.

    When accessed as attribute of TableCls(subclass of ORMBase) instance,
    it will return the value of the column/field.
    When accessed as attribute of the TableCls class,
    it will return the ColumnDescriptor itself.
    """

    def __init__(
        self,
        field_type: Type[FV],
        *constrains: str,
        type_guard: Union[Tuple[Type, ...], TYPE_CHECKER, bool] = False,
        default: Optional[FV] = None,
    ) -> None:
        # whether this field should be included in table def or not
        self._skipped = False
        self.constrains = " ".join(constrains)  # TODO: constrains validation
        self.default = field_type() if default is None else default
        self.field_type = field_type
        self.type_guard_enabled = False if type_guard is False else True

        # init type checker callable
        # default to check over the specific field type
        self.type_checker = lambda x: isinstance(x, field_type)
        if isinstance(type_guard, tuple):  # check over a list of types
            self.type_checker = lambda x: isinstance(x, type_guard)
        elif callable(type_guard):  # custom type guard function
            self.type_checker = type_guard

    @overload
    def __get__(self, obj: None, objtype: type) -> "ColumnDescriptor[FV]":
        """Descriptor accessed via class."""
        ...

    @overload
    def __get__(self, obj, objtype: type) -> FV:
        """Descriptor accessed via bound instance."""
        ...

    def __get__(self, obj, objtype=None) -> Union[FV, "ColumnDescriptor[FV]"]:
        if not self._skipped and obj is not None:
            if isinstance(obj, type):
                return self  # bound inst is type, treated same as accessed via class
            return getattr(obj, self._private_name)  # access via instance
        return self  # access via class, return the descriptor

    def __set__(self, obj, value: Any) -> None:
        if self._skipped:
            return
        # handle dataclass's default value setting behavior and NULL type assignment
        if isinstance(value, type(self)) or self.field_type == NULL_TYPE:
            return setattr(obj, self._private_name, self.default)
        # use default value if value is None and field type is not NULL
        if value is None and self.field_type != NULL_TYPE:
            value = self.default
        # handle normal value setting
        if self.type_guard_enabled and not self.type_checker(value):
            raise TypeError(f"type_guard: expect {self.field_type}, get {type(value)}")
        # apply type conversion before assign
        setattr(obj, self._private_name, self.field_type(value))

    def __set_name__(self, owner: type, name: str):
        self.owner = owner
        try:
            self._index = list(owner.__annotations__).index(name)
        except (AttributeError, ValueError):
            self._skipped = True  # skipped due to annotation missing
        self._field_name = name
        self._private_name = f"_{owner.__name__}_{name}"

    @property
    def name(self) -> str:
        return self._field_name

    @property
    def index(self) -> int:
        return self._index

    def check_type(self, value: Any) -> bool:
        return self.type_checker(value)


class ORMeta(type):
    """This metaclass is for generating customized <TableCls>."""

    def __new__(cls, cls_name: str, bases: Tuple[type, ...], classdict: Dict[str, Any]):
        new_cls: type = super().__new__(cls, cls_name, bases, classdict)
        if len(new_cls.__mro__) > 2:  # <TableCls>, ORMBase, object
            # we will define our own eq and hash logics, disable dataclass'
            # eq method and hash method generation
            return dataclass(eq=False, unsafe_hash=False)(new_cls)
        else:  # ORMBase, object
            return new_cls


class ORMBase(metaclass=ORMeta):
    """Base class for defining a sqlite3 table programatically.

    Subclass of this base class is also a subclass of dataclass.
    """

    @classmethod
    def row_to_meta(cls, row: "Union[sqlite3.Row, Dict[str, Any], Tuple[Any]]"):
        parsed = {}
        for field in fields(cls):
            try:
                col: ColumnDescriptor = getattr(cls, field.name)
                if isinstance(row, tuple):
                    parsed[col.name] = row[col.index]
                else:
                    parsed[col.name] = row[col.name]
            except (IndexError, KeyError):
                continue  # silently ignore unknonw input fields
        return cls(**parsed)

    @classmethod
    def get_create_table_stmt(cls, table_name: str) -> str:
        """Generate the sqlite query statement to create the defined table in database.

        Args:
            table_name: the name of table to be created

        Returns:
            query statement to create the table defined by this class.
        """
        _col_descriptors: List[ColumnDescriptor] = [
            getattr(cls, field.name) for field in fields(cls)
        ]
        with StringIO() as buffer:
            buffer.write(f"CREATE TABLE {table_name}")
            buffer.write("(")
            buffer.write(
                ", ".join([f"{col.name} {col.constrains}" for col in _col_descriptors])
            )
            buffer.write(")")
            return buffer.getvalue()

    @classmethod
    def contains_field(cls, _input: Union[str, ColumnDescriptor]) -> bool:
        """Check if this table contains field indicated by <_input>."""
        if isinstance(_input, ColumnDescriptor):
            return _input.owner.__name__ == cls.__name__
        return isinstance(getattr(cls, _input), ColumnDescriptor)

    @classmethod
    def get_shape(cls) -> str:
        """Used by insert row query."""
        return ",".join(["?"] * len(fields(cls)))

    def __hash__(self) -> int:
        """compute the hash with all stored fields' value."""
        return hash(astuple(self))

    def __eq__(self, __o: object) -> bool:
        if not isinstance(__o, self.__class__):
            return False
        for field in fields(self):
            field_name = field.name
            if getattr(self, field_name) != getattr(__o, field_name):
                return False
        return True

    def astuple(self) -> Tuple[SQLITE_DATATYPES, ...]:
        return astuple(self)

    def asdict(self) -> Dict[str, SQLITE_DATATYPES]:
        return asdict(self)
