import pytest
from google.protobuf import message as _message
from typing import Any, Type

from otaclient.app.proto import wrapper
from otaclient.app.proto import v2


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
    assert _directly_init[field_name] == field_value

    # test wrap
    _data = pb_cls(**{field_name: field_value})
    _wrapped = wrapper_cls.wrap(_data)
    assert _directly_init == _wrapped
    # test unwrap
    assert _data == _wrapped.unwrap()
    # test export_pb
    _exported = _wrapped.export_pb()
    assert _data == _exported

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
            v2.RollbackResponse,
            wrapper.RollbackResponse,
            "ecu",
            wrapper.RollbackResponseEcu(),
        ),
        (v2.StatusResponse, wrapper.StatusResponse, "ecu", wrapper.StatusResponseEcu()),
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
    # test unwrap
    assert _data == _wrapped.unwrap()
    # test export_pb
    _exported = _wrapped.export_pb()
    assert _data == _exported

    # test attrs proxy
    ## getattr
    _wrapped = wrapper_cls()
    getattr(_wrapped, field_name).append(value.data)
    assert _wrapped.data == _data
    ## getitem
    _wrapped = wrapper_cls()
    _wrapped[field_name].append(value.data)
    assert _wrapped.data == _data


# TODO: test helper methods for RollbackResponse, StatusProgress and UpdateResponse


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
    assert _proxied == getattr(wrapper_cls, enum_field_name)
    assert _origin == _proxied  # test directly comparing to pb types
    assert _origin == _proxied.value
    assert _origin == _proxied.export_pb()


# specific types test files


def test_Status():
    _progress = wrapper.StatusProgress(total_regular_files=123456)
    _failure, _failure_reason = wrapper.FailureType.RECOVERABLE, "recoverable"
    _status = wrapper.Status(
        progress=_progress.data,
        failure=_failure.value,
        failure_reason=_failure_reason,
    )

    # test helper methods of _status
    assert _status.get_progress() == _progress
    assert _status.get_failure() == (_failure, _failure_reason)


def test_StatusResponse():
    _ecu_status = wrapper.Status(
        failure=wrapper.FailureType.NO_FAILURE.value,
        failure_reason="no_failure",
        version="789.x",
    )
    _mainecu = wrapper.StatusResponseEcu(
        ecu_id="autoware",
        result=wrapper.FailureType.NO_FAILURE.value,
        status=_ecu_status.data,
    )
    _p1 = wrapper.StatusResponseEcu(
        ecu_id="p1",
        result=wrapper.FailureType.NO_FAILURE.value,
        status=_ecu_status.data,
    )

    _status = wrapper.StatusResponse(available_ecu_ids=["autoware", "p1"])
    _status_to_merge = wrapper.StatusResponse(available_ecu_ids=["p1"])
    # test add_ecu method
    _status.add_ecu(_mainecu)
    _status_to_merge.add_ecu(_p1)
    # test merge_from method
    _status.merge_from(_status_to_merge)
    # test get_ecu_status
    assert _status.get_ecu_status("autoware") == (
        "autoware",
        wrapper.FailureType.NO_FAILURE,
        _ecu_status,
    )
    # test iter_ecu_staus
    for _ecu_id, _ecu_result, _ecu_status in _status.iter_ecu_status():
        if _ecu_id == "autoware":
            assert _ecu_result == wrapper.FailureType.NO_FAILURE, _mainecu.status
        elif _ecu_id == "p1":
            assert _ecu_result == wrapper.FailureType.NO_FAILURE, _p1.status
        else:
            assert False

    # test update_available_ecu_ids
    _s1 = wrapper.StatusResponse()
    _s2 = wrapper.StatusResponse(available_ecu_ids=["autoware", "p1"])
    _s1.update_available_ecu_ids("autoware", "p1")
    assert _s1.available_ecu_ids == _s2.available_ecu_ids


def test_UpdateRequest():
    _ecu_id = "autoware"
    _mainecu = wrapper.UpdateRequestEcu(ecu_id=_ecu_id, version="789.x")
    _request = wrapper.UpdateRequest()
    _request.ecu.append(_mainecu.data)

    assert _request.find_update_meta(_ecu_id) == _mainecu
    _itered = list(_request.iter_update_meta())
    assert _itered[0] == _mainecu
