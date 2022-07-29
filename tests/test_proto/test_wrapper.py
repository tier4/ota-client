from typing import Any, Type
from google.protobuf import message as _message

import pytest

from app.proto import wrapper
from app.proto import v2


@pytest.mark.parametrize(
    "pb_cls, wrapper_cls, field_name, field_value",
    (
        (v2.RollbackRequestEcu, wrapper.RollbackRequestEcu, "ecu_id", "autoware"),
        (v2.RollbackResponseEcu, wrapper.RollbackResponseEcu, "ecu_id", "autoware"),
        (v2.Status, wrapper.Status, "status", v2.INITIALIZED),
        (v2.StatusProgress, wrapper.StatusProgress, "file_size_processed_copy", 123456),
        (v2.StatusResponseEcu, wrapper.StatusResponseEcu, "result", v2.NO_FAILURE),
        (v2.UpdateRequestEcu, wrapper.UpdateRequestEcu, "version", "789.x"),
        (v2.UpdateResponseEcu, wrapper.UpdateResponseEcu, "ecu_id", "autoware"),
    ),
)
def test_message_wrapper_normal_field(
    pb_cls: Type[_message.Message],
    wrapper_cls: Type[wrapper.MessageWrapperProtocol],
    field_name: str,
    field_value: Any,
):
    # test directly init
    _directly_init = wrapper_cls(**{field_name: field_value})  # type: ignore
    assert getattr(_directly_init, field_name) == field_value

    # test wrap
    _data = pb_cls(**{field_name: field_value})
    _wrapped = wrapper_cls.wrap(_data)
    assert _directly_init.data == _wrapped.data
    assert _wrapped.data is _data  # wrap
    # test unwrap
    assert _data is _wrapped.unwrap()
    # test export_pb
    _exported = _wrapped.export_pb()
    assert _data == _exported and _data is not _exported

    # test copy
    _copied = _wrapped.copy()
    # NOTE: the underlaying data is also copied
    assert _copied.data == _wrapped.data and _copied.data is not _wrapped.data

    # test attrs proxy
    _wrapped = wrapper_cls()
    setattr(_wrapped, field_name, field_value)
    assert getattr(_wrapped, field_name) == field_value
    assert getattr(_wrapped.data, field_name) == field_value
    assert _wrapped[field_name] == field_value


@pytest.mark.parametrize(
    "pb_cls, wrapper_cls, field_name, value",
    (
        (
            v2.RollbackRequest,
            wrapper.RollbackRequest,
            "ecu",
            wrapper.RollbackRequestEcu(),
        ),
        (
            v2.RollbackResponse,
            wrapper.RollbackResponse,
            "ecu",
            wrapper.RollbackResponseEcu(),
        ),
        (v2.UpdateRequest, wrapper.UpdateRequest, "ecu", wrapper.UpdateRequestEcu()),
        (v2.UpdateResponse, wrapper.UpdateResponse, "ecu", wrapper.UpdateResponseEcu()),
    ),
)
def test_message_wrapper_repeated_field(
    pb_cls: Type[_message.Message],
    wrapper_cls: Type[wrapper.MessageWrapperProtocol],
    field_name: str,
    value: wrapper.MessageWrapperProtocol,
):
    # prepare data
    _data = pb_cls()
    _ecu = _data.ecu.add()  # type: ignore
    _ecu.CopyFrom(value.data)  # type: ignore

    # test directly init
    _directly_init = wrapper_cls()
    _directly_init.add_ecu(value)
    assert _directly_init.data == _data

    # test wrap
    _wrapped = wrapper_cls.wrap(_data)
    assert _wrapped.data is _data  # wrap
    # test unwrap
    assert _data is _wrapped.unwrap()
    # test export_pb
    _exported = _wrapped.export_pb()
    assert _data == _exported and _data is not _exported

    # test copy
    _copied = _wrapped.copy()
    # NOTE: the underlaying data is also copied
    assert _copied.data == _wrapped.data and _copied.data is not _wrapped.data

    # test attrs proxy
    ## getattr
    _wrapped = wrapper_cls()
    _field = getattr(_wrapped, field_name)
    _ecu = _field.add()
    _ecu.CopyFrom(value.data)
    assert _wrapped.data == _data
    ## getitem
    _wrapped = wrapper_cls()
    _field = _wrapped[field_name]
    _ecu = _field.add()
    _ecu.CopyFrom(value.data)
    assert _wrapped.data == _data


@pytest.mark.parametrize(
    "pb_cls, wrapper_cls, enum_field_name",
    (
        (v2.FailureType, wrapper.FailureType, "NO_FAILURE"),
        (v2.StatusOta, wrapper.StatusOta, "UPDATING"),
        (v2.StatusProgressPhase, wrapper.StatusProgressPhase, "SYMLINK"),
    ),
)
def test_enum_wrapper(
    pb_cls, wrapper_cls: Type[wrapper.EnumWrapper], enum_field_name: str
):
    _origin = getattr(pb_cls, enum_field_name)
    _proxied = wrapper_cls[enum_field_name]
    assert _origin == _proxied  # test directly comparing to pb types
    assert _origin == _proxied.value
    assert _origin == _proxied.export_pb()
