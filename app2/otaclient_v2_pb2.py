# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: otaclient_v2.proto
"""Generated protocol buffer code."""
from google.protobuf.internal import enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


DESCRIPTOR = _descriptor.FileDescriptor(
    name="otaclient_v2.proto",
    package="OtaClientV2",
    syntax="proto3",
    serialized_options=b"\242\002\003OTA",
    create_key=_descriptor._internal_create_key,
    serialized_pb=b'\n\x12otaclient_v2.proto\x12\x0bOtaClientV2"Q\n\x10UpdateRequestEcu\x12\x0e\n\x06\x65\x63u_id\x18\x01 \x01(\t\x12\x0f\n\x07version\x18\x02 \x01(\t\x12\x0b\n\x03url\x18\x03 \x01(\t\x12\x0f\n\x07\x63ookies\x18\x04 \x01(\t";\n\rUpdateRequest\x12*\n\x03\x65\x63u\x18\x01 \x03(\x0b\x32\x1d.OtaClientV2.UpdateRequestEcu"H\n\x11UpdateResponseEcu\x12\x0e\n\x06\x65\x63u_id\x18\x01 \x01(\t\x12#\n\x06result\x18\x02 \x01(\x0e\x32\x13.OtaClientV2.Result"=\n\x0eUpdateResponse\x12+\n\x03\x65\x63u\x18\x01 \x03(\x0b\x32\x1e.OtaClientV2.UpdateResponseEcu"$\n\x12RollbackRequestEcu\x12\x0e\n\x06\x65\x63u_id\x18\x01 \x01(\t"?\n\x0fRollbackRequest\x12,\n\x03\x65\x63u\x18\x01 \x03(\x0b\x32\x1f.OtaClientV2.RollbackRequestEcu"J\n\x13RollbackResponseEcu\x12\x0e\n\x06\x65\x63u_id\x18\x01 \x01(\t\x12#\n\x06result\x18\x02 \x01(\x0e\x32\x13.OtaClientV2.Result"A\n\x10RollbackResponse\x12-\n\x03\x65\x63u\x18\x01 \x03(\x0b\x32 .OtaClientV2.RollbackResponseEcu"\x0f\n\rStatusRequest"\x93\x01\n\x0eStatusProgress\x12/\n\x05phase\x18\x01 \x01(\x0e\x32 .OtaClientV2.StatusProgressPhase\x12%\n\x1dtotal_number_of_regular_files\x18\x02 \x01(\r\x12)\n!number_of_regular_files_processed\x18\x03 \x01(\r"\xf5\x01\n\x11StatusResponseEcu\x12\x0e\n\x06\x65\x63u_id\x18\x01 \x01(\t\x12#\n\x06result\x18\x02 \x01(\x0e\x32\x13.OtaClientV2.Result\x12&\n\x06status\x18\x03 \x01(\x0e\x32\x16.OtaClientV2.StatusOta\x12+\n\x07\x66\x61ilure\x18\x04 \x01(\x0e\x32\x1a.OtaClientV2.StatusFailure\x12\x16\n\x0e\x66\x61ilure_reason\x18\x05 \x01(\t\x12\x0f\n\x07version\x18\x06 \x01(\t\x12-\n\x08progress\x18\x07 \x01(\x0b\x32\x1b.OtaClientV2.StatusProgress"=\n\x0eStatusResponse\x12+\n\x03\x65\x63u\x18\x01 \x03(\x0b\x32\x1e.OtaClientV2.StatusResponseEcu*:\n\x06Result\x12\x06\n\x02OK\x10\x00\x12\x14\n\x10\x45RROR_OTA_STATUS\x10\x01\x12\x12\n\rERROR_UNKNOWN\x10\xff\x01*k\n\tStatusOta\x12\x0f\n\x0bINITIALIZED\x10\x00\x12\x0b\n\x07SUCCESS\x10\x01\x12\x0b\n\x07\x46\x41ILURE\x10\x02\x12\x0c\n\x08UPDATING\x10\x03\x12\x0f\n\x0bROLLBACKING\x10\x04\x12\x14\n\x10ROLLBACK_FAILURE\x10\x05*3\n\rStatusFailure\x12\x0f\n\x0bRECOVERABLE\x10\x00\x12\x11\n\rUNRECOVERABLE\x10\x01*h\n\x13StatusProgressPhase\x12\x0c\n\x08METADATA\x10\x00\x12\r\n\tDIRECTORY\x10\x01\x12\x0b\n\x07SYMLINK\x10\x02\x12\x0b\n\x07REGULAR\x10\x03\x12\x0e\n\nPERSISTENT\x10\x04\x12\n\n\x06REBOOT\x10\x05\x32\xe7\x01\n\x10OtaClientService\x12\x43\n\x06Update\x12\x1a.OtaClientV2.UpdateRequest\x1a\x1b.OtaClientV2.UpdateResponse"\x00\x12I\n\x08Rollback\x12\x1c.OtaClientV2.RollbackRequest\x1a\x1d.OtaClientV2.RollbackResponse"\x00\x12\x43\n\x06Status\x12\x1a.OtaClientV2.StatusRequest\x1a\x1b.OtaClientV2.StatusResponse"\x00\x42\x06\xa2\x02\x03OTAb\x06proto3',
)

_RESULT = _descriptor.EnumDescriptor(
    name="Result",
    full_name="OtaClientV2.Result",
    filename=None,
    file=DESCRIPTOR,
    create_key=_descriptor._internal_create_key,
    values=[
        _descriptor.EnumValueDescriptor(
            name="OK",
            index=0,
            number=0,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="ERROR_OTA_STATUS",
            index=1,
            number=1,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="ERROR_UNKNOWN",
            index=2,
            number=255,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
    ],
    containing_type=None,
    serialized_options=None,
    serialized_start=1040,
    serialized_end=1098,
)
_sym_db.RegisterEnumDescriptor(_RESULT)

Result = enum_type_wrapper.EnumTypeWrapper(_RESULT)
_STATUSOTA = _descriptor.EnumDescriptor(
    name="StatusOta",
    full_name="OtaClientV2.StatusOta",
    filename=None,
    file=DESCRIPTOR,
    create_key=_descriptor._internal_create_key,
    values=[
        _descriptor.EnumValueDescriptor(
            name="INITIALIZED",
            index=0,
            number=0,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="SUCCESS",
            index=1,
            number=1,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="FAILURE",
            index=2,
            number=2,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="UPDATING",
            index=3,
            number=3,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="ROLLBACKING",
            index=4,
            number=4,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="ROLLBACK_FAILURE",
            index=5,
            number=5,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
    ],
    containing_type=None,
    serialized_options=None,
    serialized_start=1100,
    serialized_end=1207,
)
_sym_db.RegisterEnumDescriptor(_STATUSOTA)

StatusOta = enum_type_wrapper.EnumTypeWrapper(_STATUSOTA)
_STATUSFAILURE = _descriptor.EnumDescriptor(
    name="StatusFailure",
    full_name="OtaClientV2.StatusFailure",
    filename=None,
    file=DESCRIPTOR,
    create_key=_descriptor._internal_create_key,
    values=[
        _descriptor.EnumValueDescriptor(
            name="RECOVERABLE",
            index=0,
            number=0,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="UNRECOVERABLE",
            index=1,
            number=1,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
    ],
    containing_type=None,
    serialized_options=None,
    serialized_start=1209,
    serialized_end=1260,
)
_sym_db.RegisterEnumDescriptor(_STATUSFAILURE)

StatusFailure = enum_type_wrapper.EnumTypeWrapper(_STATUSFAILURE)
_STATUSPROGRESSPHASE = _descriptor.EnumDescriptor(
    name="StatusProgressPhase",
    full_name="OtaClientV2.StatusProgressPhase",
    filename=None,
    file=DESCRIPTOR,
    create_key=_descriptor._internal_create_key,
    values=[
        _descriptor.EnumValueDescriptor(
            name="METADATA",
            index=0,
            number=0,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="DIRECTORY",
            index=1,
            number=1,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="SYMLINK",
            index=2,
            number=2,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="REGULAR",
            index=3,
            number=3,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="PERSISTENT",
            index=4,
            number=4,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="REBOOT",
            index=5,
            number=5,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
    ],
    containing_type=None,
    serialized_options=None,
    serialized_start=1262,
    serialized_end=1366,
)
_sym_db.RegisterEnumDescriptor(_STATUSPROGRESSPHASE)

StatusProgressPhase = enum_type_wrapper.EnumTypeWrapper(_STATUSPROGRESSPHASE)
OK = 0
ERROR_OTA_STATUS = 1
ERROR_UNKNOWN = 255
INITIALIZED = 0
SUCCESS = 1
FAILURE = 2
UPDATING = 3
ROLLBACKING = 4
ROLLBACK_FAILURE = 5
RECOVERABLE = 0
UNRECOVERABLE = 1
METADATA = 0
DIRECTORY = 1
SYMLINK = 2
REGULAR = 3
PERSISTENT = 4
REBOOT = 5


_UPDATEREQUESTECU = _descriptor.Descriptor(
    name="UpdateRequestEcu",
    full_name="OtaClientV2.UpdateRequestEcu",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[
        _descriptor.FieldDescriptor(
            name="ecu_id",
            full_name="OtaClientV2.UpdateRequestEcu.ecu_id",
            index=0,
            number=1,
            type=9,
            cpp_type=9,
            label=1,
            has_default_value=False,
            default_value=b"".decode("utf-8"),
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.FieldDescriptor(
            name="version",
            full_name="OtaClientV2.UpdateRequestEcu.version",
            index=1,
            number=2,
            type=9,
            cpp_type=9,
            label=1,
            has_default_value=False,
            default_value=b"".decode("utf-8"),
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.FieldDescriptor(
            name="url",
            full_name="OtaClientV2.UpdateRequestEcu.url",
            index=2,
            number=3,
            type=9,
            cpp_type=9,
            label=1,
            has_default_value=False,
            default_value=b"".decode("utf-8"),
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.FieldDescriptor(
            name="cookies",
            full_name="OtaClientV2.UpdateRequestEcu.cookies",
            index=3,
            number=4,
            type=9,
            cpp_type=9,
            label=1,
            has_default_value=False,
            default_value=b"".decode("utf-8"),
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
    ],
    extensions=[],
    nested_types=[],
    enum_types=[],
    serialized_options=None,
    is_extendable=False,
    syntax="proto3",
    extension_ranges=[],
    oneofs=[],
    serialized_start=35,
    serialized_end=116,
)


_UPDATEREQUEST = _descriptor.Descriptor(
    name="UpdateRequest",
    full_name="OtaClientV2.UpdateRequest",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[
        _descriptor.FieldDescriptor(
            name="ecu",
            full_name="OtaClientV2.UpdateRequest.ecu",
            index=0,
            number=1,
            type=11,
            cpp_type=10,
            label=3,
            has_default_value=False,
            default_value=[],
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
    ],
    extensions=[],
    nested_types=[],
    enum_types=[],
    serialized_options=None,
    is_extendable=False,
    syntax="proto3",
    extension_ranges=[],
    oneofs=[],
    serialized_start=118,
    serialized_end=177,
)


_UPDATERESPONSEECU = _descriptor.Descriptor(
    name="UpdateResponseEcu",
    full_name="OtaClientV2.UpdateResponseEcu",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[
        _descriptor.FieldDescriptor(
            name="ecu_id",
            full_name="OtaClientV2.UpdateResponseEcu.ecu_id",
            index=0,
            number=1,
            type=9,
            cpp_type=9,
            label=1,
            has_default_value=False,
            default_value=b"".decode("utf-8"),
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.FieldDescriptor(
            name="result",
            full_name="OtaClientV2.UpdateResponseEcu.result",
            index=1,
            number=2,
            type=14,
            cpp_type=8,
            label=1,
            has_default_value=False,
            default_value=0,
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
    ],
    extensions=[],
    nested_types=[],
    enum_types=[],
    serialized_options=None,
    is_extendable=False,
    syntax="proto3",
    extension_ranges=[],
    oneofs=[],
    serialized_start=179,
    serialized_end=251,
)


_UPDATERESPONSE = _descriptor.Descriptor(
    name="UpdateResponse",
    full_name="OtaClientV2.UpdateResponse",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[
        _descriptor.FieldDescriptor(
            name="ecu",
            full_name="OtaClientV2.UpdateResponse.ecu",
            index=0,
            number=1,
            type=11,
            cpp_type=10,
            label=3,
            has_default_value=False,
            default_value=[],
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
    ],
    extensions=[],
    nested_types=[],
    enum_types=[],
    serialized_options=None,
    is_extendable=False,
    syntax="proto3",
    extension_ranges=[],
    oneofs=[],
    serialized_start=253,
    serialized_end=314,
)


_ROLLBACKREQUESTECU = _descriptor.Descriptor(
    name="RollbackRequestEcu",
    full_name="OtaClientV2.RollbackRequestEcu",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[
        _descriptor.FieldDescriptor(
            name="ecu_id",
            full_name="OtaClientV2.RollbackRequestEcu.ecu_id",
            index=0,
            number=1,
            type=9,
            cpp_type=9,
            label=1,
            has_default_value=False,
            default_value=b"".decode("utf-8"),
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
    ],
    extensions=[],
    nested_types=[],
    enum_types=[],
    serialized_options=None,
    is_extendable=False,
    syntax="proto3",
    extension_ranges=[],
    oneofs=[],
    serialized_start=316,
    serialized_end=352,
)


_ROLLBACKREQUEST = _descriptor.Descriptor(
    name="RollbackRequest",
    full_name="OtaClientV2.RollbackRequest",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[
        _descriptor.FieldDescriptor(
            name="ecu",
            full_name="OtaClientV2.RollbackRequest.ecu",
            index=0,
            number=1,
            type=11,
            cpp_type=10,
            label=3,
            has_default_value=False,
            default_value=[],
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
    ],
    extensions=[],
    nested_types=[],
    enum_types=[],
    serialized_options=None,
    is_extendable=False,
    syntax="proto3",
    extension_ranges=[],
    oneofs=[],
    serialized_start=354,
    serialized_end=417,
)


_ROLLBACKRESPONSEECU = _descriptor.Descriptor(
    name="RollbackResponseEcu",
    full_name="OtaClientV2.RollbackResponseEcu",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[
        _descriptor.FieldDescriptor(
            name="ecu_id",
            full_name="OtaClientV2.RollbackResponseEcu.ecu_id",
            index=0,
            number=1,
            type=9,
            cpp_type=9,
            label=1,
            has_default_value=False,
            default_value=b"".decode("utf-8"),
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.FieldDescriptor(
            name="result",
            full_name="OtaClientV2.RollbackResponseEcu.result",
            index=1,
            number=2,
            type=14,
            cpp_type=8,
            label=1,
            has_default_value=False,
            default_value=0,
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
    ],
    extensions=[],
    nested_types=[],
    enum_types=[],
    serialized_options=None,
    is_extendable=False,
    syntax="proto3",
    extension_ranges=[],
    oneofs=[],
    serialized_start=419,
    serialized_end=493,
)


_ROLLBACKRESPONSE = _descriptor.Descriptor(
    name="RollbackResponse",
    full_name="OtaClientV2.RollbackResponse",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[
        _descriptor.FieldDescriptor(
            name="ecu",
            full_name="OtaClientV2.RollbackResponse.ecu",
            index=0,
            number=1,
            type=11,
            cpp_type=10,
            label=3,
            has_default_value=False,
            default_value=[],
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
    ],
    extensions=[],
    nested_types=[],
    enum_types=[],
    serialized_options=None,
    is_extendable=False,
    syntax="proto3",
    extension_ranges=[],
    oneofs=[],
    serialized_start=495,
    serialized_end=560,
)


_STATUSREQUEST = _descriptor.Descriptor(
    name="StatusRequest",
    full_name="OtaClientV2.StatusRequest",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[],
    extensions=[],
    nested_types=[],
    enum_types=[],
    serialized_options=None,
    is_extendable=False,
    syntax="proto3",
    extension_ranges=[],
    oneofs=[],
    serialized_start=562,
    serialized_end=577,
)


_STATUSPROGRESS = _descriptor.Descriptor(
    name="StatusProgress",
    full_name="OtaClientV2.StatusProgress",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[
        _descriptor.FieldDescriptor(
            name="phase",
            full_name="OtaClientV2.StatusProgress.phase",
            index=0,
            number=1,
            type=14,
            cpp_type=8,
            label=1,
            has_default_value=False,
            default_value=0,
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.FieldDescriptor(
            name="total_number_of_regular_files",
            full_name="OtaClientV2.StatusProgress.total_number_of_regular_files",
            index=1,
            number=2,
            type=13,
            cpp_type=3,
            label=1,
            has_default_value=False,
            default_value=0,
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.FieldDescriptor(
            name="number_of_regular_files_processed",
            full_name="OtaClientV2.StatusProgress.number_of_regular_files_processed",
            index=2,
            number=3,
            type=13,
            cpp_type=3,
            label=1,
            has_default_value=False,
            default_value=0,
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
    ],
    extensions=[],
    nested_types=[],
    enum_types=[],
    serialized_options=None,
    is_extendable=False,
    syntax="proto3",
    extension_ranges=[],
    oneofs=[],
    serialized_start=580,
    serialized_end=727,
)


_STATUSRESPONSEECU = _descriptor.Descriptor(
    name="StatusResponseEcu",
    full_name="OtaClientV2.StatusResponseEcu",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[
        _descriptor.FieldDescriptor(
            name="ecu_id",
            full_name="OtaClientV2.StatusResponseEcu.ecu_id",
            index=0,
            number=1,
            type=9,
            cpp_type=9,
            label=1,
            has_default_value=False,
            default_value=b"".decode("utf-8"),
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.FieldDescriptor(
            name="result",
            full_name="OtaClientV2.StatusResponseEcu.result",
            index=1,
            number=2,
            type=14,
            cpp_type=8,
            label=1,
            has_default_value=False,
            default_value=0,
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.FieldDescriptor(
            name="status",
            full_name="OtaClientV2.StatusResponseEcu.status",
            index=2,
            number=3,
            type=14,
            cpp_type=8,
            label=1,
            has_default_value=False,
            default_value=0,
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.FieldDescriptor(
            name="failure",
            full_name="OtaClientV2.StatusResponseEcu.failure",
            index=3,
            number=4,
            type=14,
            cpp_type=8,
            label=1,
            has_default_value=False,
            default_value=0,
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.FieldDescriptor(
            name="failure_reason",
            full_name="OtaClientV2.StatusResponseEcu.failure_reason",
            index=4,
            number=5,
            type=9,
            cpp_type=9,
            label=1,
            has_default_value=False,
            default_value=b"".decode("utf-8"),
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.FieldDescriptor(
            name="version",
            full_name="OtaClientV2.StatusResponseEcu.version",
            index=5,
            number=6,
            type=9,
            cpp_type=9,
            label=1,
            has_default_value=False,
            default_value=b"".decode("utf-8"),
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.FieldDescriptor(
            name="progress",
            full_name="OtaClientV2.StatusResponseEcu.progress",
            index=6,
            number=7,
            type=11,
            cpp_type=10,
            label=1,
            has_default_value=False,
            default_value=None,
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
    ],
    extensions=[],
    nested_types=[],
    enum_types=[],
    serialized_options=None,
    is_extendable=False,
    syntax="proto3",
    extension_ranges=[],
    oneofs=[],
    serialized_start=730,
    serialized_end=975,
)


_STATUSRESPONSE = _descriptor.Descriptor(
    name="StatusResponse",
    full_name="OtaClientV2.StatusResponse",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[
        _descriptor.FieldDescriptor(
            name="ecu",
            full_name="OtaClientV2.StatusResponse.ecu",
            index=0,
            number=1,
            type=11,
            cpp_type=10,
            label=3,
            has_default_value=False,
            default_value=[],
            message_type=None,
            enum_type=None,
            containing_type=None,
            is_extension=False,
            extension_scope=None,
            serialized_options=None,
            file=DESCRIPTOR,
            create_key=_descriptor._internal_create_key,
        ),
    ],
    extensions=[],
    nested_types=[],
    enum_types=[],
    serialized_options=None,
    is_extendable=False,
    syntax="proto3",
    extension_ranges=[],
    oneofs=[],
    serialized_start=977,
    serialized_end=1038,
)

_UPDATEREQUEST.fields_by_name["ecu"].message_type = _UPDATEREQUESTECU
_UPDATERESPONSEECU.fields_by_name["result"].enum_type = _RESULT
_UPDATERESPONSE.fields_by_name["ecu"].message_type = _UPDATERESPONSEECU
_ROLLBACKREQUEST.fields_by_name["ecu"].message_type = _ROLLBACKREQUESTECU
_ROLLBACKRESPONSEECU.fields_by_name["result"].enum_type = _RESULT
_ROLLBACKRESPONSE.fields_by_name["ecu"].message_type = _ROLLBACKRESPONSEECU
_STATUSPROGRESS.fields_by_name["phase"].enum_type = _STATUSPROGRESSPHASE
_STATUSRESPONSEECU.fields_by_name["result"].enum_type = _RESULT
_STATUSRESPONSEECU.fields_by_name["status"].enum_type = _STATUSOTA
_STATUSRESPONSEECU.fields_by_name["failure"].enum_type = _STATUSFAILURE
_STATUSRESPONSEECU.fields_by_name["progress"].message_type = _STATUSPROGRESS
_STATUSRESPONSE.fields_by_name["ecu"].message_type = _STATUSRESPONSEECU
DESCRIPTOR.message_types_by_name["UpdateRequestEcu"] = _UPDATEREQUESTECU
DESCRIPTOR.message_types_by_name["UpdateRequest"] = _UPDATEREQUEST
DESCRIPTOR.message_types_by_name["UpdateResponseEcu"] = _UPDATERESPONSEECU
DESCRIPTOR.message_types_by_name["UpdateResponse"] = _UPDATERESPONSE
DESCRIPTOR.message_types_by_name["RollbackRequestEcu"] = _ROLLBACKREQUESTECU
DESCRIPTOR.message_types_by_name["RollbackRequest"] = _ROLLBACKREQUEST
DESCRIPTOR.message_types_by_name["RollbackResponseEcu"] = _ROLLBACKRESPONSEECU
DESCRIPTOR.message_types_by_name["RollbackResponse"] = _ROLLBACKRESPONSE
DESCRIPTOR.message_types_by_name["StatusRequest"] = _STATUSREQUEST
DESCRIPTOR.message_types_by_name["StatusProgress"] = _STATUSPROGRESS
DESCRIPTOR.message_types_by_name["StatusResponseEcu"] = _STATUSRESPONSEECU
DESCRIPTOR.message_types_by_name["StatusResponse"] = _STATUSRESPONSE
DESCRIPTOR.enum_types_by_name["Result"] = _RESULT
DESCRIPTOR.enum_types_by_name["StatusOta"] = _STATUSOTA
DESCRIPTOR.enum_types_by_name["StatusFailure"] = _STATUSFAILURE
DESCRIPTOR.enum_types_by_name["StatusProgressPhase"] = _STATUSPROGRESSPHASE
_sym_db.RegisterFileDescriptor(DESCRIPTOR)

UpdateRequestEcu = _reflection.GeneratedProtocolMessageType(
    "UpdateRequestEcu",
    (_message.Message,),
    {
        "DESCRIPTOR": _UPDATEREQUESTECU,
        "__module__": "otaclient_v2_pb2"
        # @@protoc_insertion_point(class_scope:OtaClientV2.UpdateRequestEcu)
    },
)
_sym_db.RegisterMessage(UpdateRequestEcu)

UpdateRequest = _reflection.GeneratedProtocolMessageType(
    "UpdateRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _UPDATEREQUEST,
        "__module__": "otaclient_v2_pb2"
        # @@protoc_insertion_point(class_scope:OtaClientV2.UpdateRequest)
    },
)
_sym_db.RegisterMessage(UpdateRequest)

UpdateResponseEcu = _reflection.GeneratedProtocolMessageType(
    "UpdateResponseEcu",
    (_message.Message,),
    {
        "DESCRIPTOR": _UPDATERESPONSEECU,
        "__module__": "otaclient_v2_pb2"
        # @@protoc_insertion_point(class_scope:OtaClientV2.UpdateResponseEcu)
    },
)
_sym_db.RegisterMessage(UpdateResponseEcu)

UpdateResponse = _reflection.GeneratedProtocolMessageType(
    "UpdateResponse",
    (_message.Message,),
    {
        "DESCRIPTOR": _UPDATERESPONSE,
        "__module__": "otaclient_v2_pb2"
        # @@protoc_insertion_point(class_scope:OtaClientV2.UpdateResponse)
    },
)
_sym_db.RegisterMessage(UpdateResponse)

RollbackRequestEcu = _reflection.GeneratedProtocolMessageType(
    "RollbackRequestEcu",
    (_message.Message,),
    {
        "DESCRIPTOR": _ROLLBACKREQUESTECU,
        "__module__": "otaclient_v2_pb2"
        # @@protoc_insertion_point(class_scope:OtaClientV2.RollbackRequestEcu)
    },
)
_sym_db.RegisterMessage(RollbackRequestEcu)

RollbackRequest = _reflection.GeneratedProtocolMessageType(
    "RollbackRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _ROLLBACKREQUEST,
        "__module__": "otaclient_v2_pb2"
        # @@protoc_insertion_point(class_scope:OtaClientV2.RollbackRequest)
    },
)
_sym_db.RegisterMessage(RollbackRequest)

RollbackResponseEcu = _reflection.GeneratedProtocolMessageType(
    "RollbackResponseEcu",
    (_message.Message,),
    {
        "DESCRIPTOR": _ROLLBACKRESPONSEECU,
        "__module__": "otaclient_v2_pb2"
        # @@protoc_insertion_point(class_scope:OtaClientV2.RollbackResponseEcu)
    },
)
_sym_db.RegisterMessage(RollbackResponseEcu)

RollbackResponse = _reflection.GeneratedProtocolMessageType(
    "RollbackResponse",
    (_message.Message,),
    {
        "DESCRIPTOR": _ROLLBACKRESPONSE,
        "__module__": "otaclient_v2_pb2"
        # @@protoc_insertion_point(class_scope:OtaClientV2.RollbackResponse)
    },
)
_sym_db.RegisterMessage(RollbackResponse)

StatusRequest = _reflection.GeneratedProtocolMessageType(
    "StatusRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _STATUSREQUEST,
        "__module__": "otaclient_v2_pb2"
        # @@protoc_insertion_point(class_scope:OtaClientV2.StatusRequest)
    },
)
_sym_db.RegisterMessage(StatusRequest)

StatusProgress = _reflection.GeneratedProtocolMessageType(
    "StatusProgress",
    (_message.Message,),
    {
        "DESCRIPTOR": _STATUSPROGRESS,
        "__module__": "otaclient_v2_pb2"
        # @@protoc_insertion_point(class_scope:OtaClientV2.StatusProgress)
    },
)
_sym_db.RegisterMessage(StatusProgress)

StatusResponseEcu = _reflection.GeneratedProtocolMessageType(
    "StatusResponseEcu",
    (_message.Message,),
    {
        "DESCRIPTOR": _STATUSRESPONSEECU,
        "__module__": "otaclient_v2_pb2"
        # @@protoc_insertion_point(class_scope:OtaClientV2.StatusResponseEcu)
    },
)
_sym_db.RegisterMessage(StatusResponseEcu)

StatusResponse = _reflection.GeneratedProtocolMessageType(
    "StatusResponse",
    (_message.Message,),
    {
        "DESCRIPTOR": _STATUSRESPONSE,
        "__module__": "otaclient_v2_pb2"
        # @@protoc_insertion_point(class_scope:OtaClientV2.StatusResponse)
    },
)
_sym_db.RegisterMessage(StatusResponse)


DESCRIPTOR._options = None

_OTACLIENTSERVICE = _descriptor.ServiceDescriptor(
    name="OtaClientService",
    full_name="OtaClientV2.OtaClientService",
    file=DESCRIPTOR,
    index=0,
    serialized_options=None,
    create_key=_descriptor._internal_create_key,
    serialized_start=1369,
    serialized_end=1600,
    methods=[
        _descriptor.MethodDescriptor(
            name="Update",
            full_name="OtaClientV2.OtaClientService.Update",
            index=0,
            containing_service=None,
            input_type=_UPDATEREQUEST,
            output_type=_UPDATERESPONSE,
            serialized_options=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.MethodDescriptor(
            name="Rollback",
            full_name="OtaClientV2.OtaClientService.Rollback",
            index=1,
            containing_service=None,
            input_type=_ROLLBACKREQUEST,
            output_type=_ROLLBACKRESPONSE,
            serialized_options=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.MethodDescriptor(
            name="Status",
            full_name="OtaClientV2.OtaClientService.Status",
            index=2,
            containing_service=None,
            input_type=_STATUSREQUEST,
            output_type=_STATUSRESPONSE,
            serialized_options=None,
            create_key=_descriptor._internal_create_key,
        ),
    ],
)
_sym_db.RegisterServiceDescriptor(_OTACLIENTSERVICE)

DESCRIPTOR.services_by_name["OtaClientService"] = _OTACLIENTSERVICE

# @@protoc_insertion_point(module_scope)
