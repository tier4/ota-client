# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: otaclient.proto
"""Generated protocol buffer code."""
from google.protobuf.internal import enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


DESCRIPTOR = _descriptor.FileDescriptor(
    name="otaclient.proto",
    package="OtaClient",
    syntax="proto3",
    serialized_options=b"\242\002\003OTA",
    create_key=_descriptor._internal_create_key,
    serialized_pb=b'\n\x0fotaclient.proto\x12\tOtaClient"c\n\x07\x45\x63uInfo\x12\x10\n\x08\x65\x63u_name\x18\x01 \x01(\t\x12\x10\n\x08\x65\x63u_type\x18\x02 \x01(\t\x12\x0e\n\x06\x65\x63u_id\x18\x03 \x01(\t\x12\x0f\n\x07version\x18\x04 \x01(\t\x12\x13\n\x0bindependent\x18\x05 \x01(\x08"d\n\rEcuUpdateInfo\x12$\n\x08\x65\x63u_info\x18\x01 \x01(\x0b\x32\x12.OtaClient.EcuInfo\x12\x0b\n\x03url\x18\x02 \x01(\t\x12\x10\n\x08metadata\x18\x03 \x01(\t\x12\x0e\n\x06header\x18\x04 \x01(\t"V\n\x10OtaUpdateRequest\x12\x0f\n\x07version\x18\x01 \x01(\t\x12\x31\n\x0f\x65\x63u_update_info\x18\x02 \x03(\x0b\x32\x18.OtaClient.EcuUpdateInfo"c\n\x0eOtaUpdateReply\x12+\n\x06result\x18\x01 \x01(\x0e\x32\x1b.OtaClient.UpdateResultType\x12$\n\x08\x65\x63u_info\x18\x02 \x03(\x0b\x32\x12.OtaClient.EcuInfo":\n\x12OtaRollbackRequest\x12$\n\x08\x65\x63u_info\x18\x01 \x03(\x0b\x32\x12.OtaClient.EcuInfo"g\n\x10OtaRollbackReply\x12-\n\x06result\x18\x01 \x01(\x0e\x32\x1d.OtaClient.RollbackResultType\x12$\n\x08\x65\x63u_info\x18\x02 \x03(\x0b\x32\x12.OtaClient.EcuInfo"\x12\n\x10\x45\x63uStatusRequest"j\n\x0e\x45\x63uStatusReply\x12(\n\x06status\x18\x01 \x01(\x0e\x32\x18.OtaClient.EcuStatusType\x12.\n\x0b\x62oot_status\x18\x02 \x01(\x0e\x32\x19.OtaClient.BootStatusType"\x13\n\x11\x45\x63uVersionRequest"7\n\x0f\x45\x63uVersionReply\x12$\n\x08\x65\x63u_info\x18\x01 \x03(\x0b\x32\x12.OtaClient.EcuInfo"\x12\n\x10OtaRebootRequest"\x10\n\x0eOtaRebootReply*\x8a\x01\n\x10UpdateResultType\x12\x1b\n\x17UPDATE_DOWNLOAD_SUCCESS\x10\x00\x12\x0f\n\x0bUPDATE_FAIL\x10\x01\x12\x16\n\x12UPDATE_FAIL_NO_ECU\x10\x02\x12\x18\n\x14UPDATE_DOWNLOAD_FAIL\x10\x03\x12\x16\n\x12UPDATE_REBOOT_FAIL\x10\x04*Y\n\x12RollbackResultType\x12\x14\n\x10ROLLBACK_SUCCESS\x10\x00\x12\x11\n\rROLLBACK_FAIL\x10\x01\x12\x1a\n\x16ROLLBACK_NOT_AVAILABLE\x10\x02*\xde\x01\n\rEcuStatusType\x12\x15\n\x11\x45\x43U_STATUS_NORMAL\x10\x00\x12\x17\n\x13\x45\x43U_STATUS_UPDATING\x10\x01\x12\x19\n\x15\x45\x43U_STATUS_DOWNLOADED\x10\x02\x12\x17\n\x13\x45\x43U_STATUS_ROLLBACK\x10\x03\x12\x15\n\x11\x45\x43U_STATUS_REBOOT\x10\x04\x12\x1b\n\x17\x45\x43U_STATUS_UPDATE_ERROR\x10\x05\x12\x1d\n\x19\x45\x43U_STATUS_ROLLBACK_ERROR\x10\x06\x12\x16\n\x12\x45\x43U_STATUS_UNKNOWN\x10\x07*\xb3\x01\n\x0e\x42ootStatusType\x12\x0f\n\x0bNORMAL_BOOT\x10\x00\x12\x0f\n\x0bSWITCH_BOOT\x10\x01\x12\x11\n\rROLLBACK_BOOT\x10\x02\x12\x17\n\x13SWITCHING_BOOT_FAIL\x10\x03\x12\x16\n\x12ROLLBACK_BOOT_FAIL\x10\x04\x12\x15\n\x11UPDATE_INCOMPLETE\x10\x05\x12\x17\n\x13ROLLBACK_INCOMPLETE\x10\x06\x12\x0b\n\x07UNKNOWN\x10\x07\x32\xfe\x02\n\x10OtaClientService\x12\x45\n\tOtaUpdate\x12\x1b.OtaClient.OtaUpdateRequest\x1a\x19.OtaClient.OtaUpdateReply"\x00\x12K\n\x0bOtaRollback\x12\x1d.OtaClient.OtaRollbackRequest\x1a\x1b.OtaClient.OtaRollbackReply"\x00\x12\x45\n\tOtaReboot\x12\x1b.OtaClient.OtaRebootRequest\x1a\x19.OtaClient.OtaRebootReply"\x00\x12\x45\n\tEcuStatus\x12\x1b.OtaClient.EcuStatusRequest\x1a\x19.OtaClient.EcuStatusReply"\x00\x12H\n\nEcuVersion\x12\x1c.OtaClient.EcuVersionRequest\x1a\x1a.OtaClient.EcuVersionReply"\x00\x42\x06\xa2\x02\x03OTAb\x06proto3',
)

_UPDATERESULTTYPE = _descriptor.EnumDescriptor(
    name="UpdateResultType",
    full_name="OtaClient.UpdateResultType",
    filename=None,
    file=DESCRIPTOR,
    create_key=_descriptor._internal_create_key,
    values=[
        _descriptor.EnumValueDescriptor(
            name="UPDATE_DOWNLOAD_SUCCESS",
            index=0,
            number=0,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="UPDATE_FAIL",
            index=1,
            number=1,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="UPDATE_FAIL_NO_ECU",
            index=2,
            number=2,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="UPDATE_DOWNLOAD_FAIL",
            index=3,
            number=3,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="UPDATE_REBOOT_FAIL",
            index=4,
            number=4,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
    ],
    containing_type=None,
    serialized_options=None,
    serialized_start=832,
    serialized_end=970,
)
_sym_db.RegisterEnumDescriptor(_UPDATERESULTTYPE)

UpdateResultType = enum_type_wrapper.EnumTypeWrapper(_UPDATERESULTTYPE)
_ROLLBACKRESULTTYPE = _descriptor.EnumDescriptor(
    name="RollbackResultType",
    full_name="OtaClient.RollbackResultType",
    filename=None,
    file=DESCRIPTOR,
    create_key=_descriptor._internal_create_key,
    values=[
        _descriptor.EnumValueDescriptor(
            name="ROLLBACK_SUCCESS",
            index=0,
            number=0,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="ROLLBACK_FAIL",
            index=1,
            number=1,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="ROLLBACK_NOT_AVAILABLE",
            index=2,
            number=2,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
    ],
    containing_type=None,
    serialized_options=None,
    serialized_start=972,
    serialized_end=1061,
)
_sym_db.RegisterEnumDescriptor(_ROLLBACKRESULTTYPE)

RollbackResultType = enum_type_wrapper.EnumTypeWrapper(_ROLLBACKRESULTTYPE)
_ECUSTATUSTYPE = _descriptor.EnumDescriptor(
    name="EcuStatusType",
    full_name="OtaClient.EcuStatusType",
    filename=None,
    file=DESCRIPTOR,
    create_key=_descriptor._internal_create_key,
    values=[
        _descriptor.EnumValueDescriptor(
            name="ECU_STATUS_NORMAL",
            index=0,
            number=0,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="ECU_STATUS_UPDATING",
            index=1,
            number=1,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="ECU_STATUS_DOWNLOADED",
            index=2,
            number=2,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="ECU_STATUS_ROLLBACK",
            index=3,
            number=3,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="ECU_STATUS_REBOOT",
            index=4,
            number=4,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="ECU_STATUS_UPDATE_ERROR",
            index=5,
            number=5,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="ECU_STATUS_ROLLBACK_ERROR",
            index=6,
            number=6,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="ECU_STATUS_UNKNOWN",
            index=7,
            number=7,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
    ],
    containing_type=None,
    serialized_options=None,
    serialized_start=1064,
    serialized_end=1286,
)
_sym_db.RegisterEnumDescriptor(_ECUSTATUSTYPE)

EcuStatusType = enum_type_wrapper.EnumTypeWrapper(_ECUSTATUSTYPE)
_BOOTSTATUSTYPE = _descriptor.EnumDescriptor(
    name="BootStatusType",
    full_name="OtaClient.BootStatusType",
    filename=None,
    file=DESCRIPTOR,
    create_key=_descriptor._internal_create_key,
    values=[
        _descriptor.EnumValueDescriptor(
            name="NORMAL_BOOT",
            index=0,
            number=0,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="SWITCH_BOOT",
            index=1,
            number=1,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="ROLLBACK_BOOT",
            index=2,
            number=2,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="SWITCHING_BOOT_FAIL",
            index=3,
            number=3,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="ROLLBACK_BOOT_FAIL",
            index=4,
            number=4,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="UPDATE_INCOMPLETE",
            index=5,
            number=5,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="ROLLBACK_INCOMPLETE",
            index=6,
            number=6,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.EnumValueDescriptor(
            name="UNKNOWN",
            index=7,
            number=7,
            serialized_options=None,
            type=None,
            create_key=_descriptor._internal_create_key,
        ),
    ],
    containing_type=None,
    serialized_options=None,
    serialized_start=1289,
    serialized_end=1468,
)
_sym_db.RegisterEnumDescriptor(_BOOTSTATUSTYPE)

BootStatusType = enum_type_wrapper.EnumTypeWrapper(_BOOTSTATUSTYPE)
UPDATE_DOWNLOAD_SUCCESS = 0
UPDATE_FAIL = 1
UPDATE_FAIL_NO_ECU = 2
UPDATE_DOWNLOAD_FAIL = 3
UPDATE_REBOOT_FAIL = 4
ROLLBACK_SUCCESS = 0
ROLLBACK_FAIL = 1
ROLLBACK_NOT_AVAILABLE = 2
ECU_STATUS_NORMAL = 0
ECU_STATUS_UPDATING = 1
ECU_STATUS_DOWNLOADED = 2
ECU_STATUS_ROLLBACK = 3
ECU_STATUS_REBOOT = 4
ECU_STATUS_UPDATE_ERROR = 5
ECU_STATUS_ROLLBACK_ERROR = 6
ECU_STATUS_UNKNOWN = 7
NORMAL_BOOT = 0
SWITCH_BOOT = 1
ROLLBACK_BOOT = 2
SWITCHING_BOOT_FAIL = 3
ROLLBACK_BOOT_FAIL = 4
UPDATE_INCOMPLETE = 5
ROLLBACK_INCOMPLETE = 6
UNKNOWN = 7


_ECUINFO = _descriptor.Descriptor(
    name="EcuInfo",
    full_name="OtaClient.EcuInfo",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[
        _descriptor.FieldDescriptor(
            name="ecu_name",
            full_name="OtaClient.EcuInfo.ecu_name",
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
            name="ecu_type",
            full_name="OtaClient.EcuInfo.ecu_type",
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
            name="ecu_id",
            full_name="OtaClient.EcuInfo.ecu_id",
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
            name="version",
            full_name="OtaClient.EcuInfo.version",
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
        _descriptor.FieldDescriptor(
            name="independent",
            full_name="OtaClient.EcuInfo.independent",
            index=4,
            number=5,
            type=8,
            cpp_type=7,
            label=1,
            has_default_value=False,
            default_value=False,
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
    serialized_start=30,
    serialized_end=129,
)


_ECUUPDATEINFO = _descriptor.Descriptor(
    name="EcuUpdateInfo",
    full_name="OtaClient.EcuUpdateInfo",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[
        _descriptor.FieldDescriptor(
            name="ecu_info",
            full_name="OtaClient.EcuUpdateInfo.ecu_info",
            index=0,
            number=1,
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
        _descriptor.FieldDescriptor(
            name="url",
            full_name="OtaClient.EcuUpdateInfo.url",
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
            name="metadata",
            full_name="OtaClient.EcuUpdateInfo.metadata",
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
            name="header",
            full_name="OtaClient.EcuUpdateInfo.header",
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
    serialized_start=131,
    serialized_end=231,
)


_OTAUPDATEREQUEST = _descriptor.Descriptor(
    name="OtaUpdateRequest",
    full_name="OtaClient.OtaUpdateRequest",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[
        _descriptor.FieldDescriptor(
            name="version",
            full_name="OtaClient.OtaUpdateRequest.version",
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
            name="ecu_update_info",
            full_name="OtaClient.OtaUpdateRequest.ecu_update_info",
            index=1,
            number=2,
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
    serialized_start=233,
    serialized_end=319,
)


_OTAUPDATEREPLY = _descriptor.Descriptor(
    name="OtaUpdateReply",
    full_name="OtaClient.OtaUpdateReply",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[
        _descriptor.FieldDescriptor(
            name="result",
            full_name="OtaClient.OtaUpdateReply.result",
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
            name="ecu_info",
            full_name="OtaClient.OtaUpdateReply.ecu_info",
            index=1,
            number=2,
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
    serialized_start=321,
    serialized_end=420,
)


_OTAROLLBACKREQUEST = _descriptor.Descriptor(
    name="OtaRollbackRequest",
    full_name="OtaClient.OtaRollbackRequest",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[
        _descriptor.FieldDescriptor(
            name="ecu_info",
            full_name="OtaClient.OtaRollbackRequest.ecu_info",
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
    serialized_start=422,
    serialized_end=480,
)


_OTAROLLBACKREPLY = _descriptor.Descriptor(
    name="OtaRollbackReply",
    full_name="OtaClient.OtaRollbackReply",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[
        _descriptor.FieldDescriptor(
            name="result",
            full_name="OtaClient.OtaRollbackReply.result",
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
            name="ecu_info",
            full_name="OtaClient.OtaRollbackReply.ecu_info",
            index=1,
            number=2,
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
    serialized_start=482,
    serialized_end=585,
)


_ECUSTATUSREQUEST = _descriptor.Descriptor(
    name="EcuStatusRequest",
    full_name="OtaClient.EcuStatusRequest",
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
    serialized_start=587,
    serialized_end=605,
)


_ECUSTATUSREPLY = _descriptor.Descriptor(
    name="EcuStatusReply",
    full_name="OtaClient.EcuStatusReply",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[
        _descriptor.FieldDescriptor(
            name="status",
            full_name="OtaClient.EcuStatusReply.status",
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
            name="boot_status",
            full_name="OtaClient.EcuStatusReply.boot_status",
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
    serialized_start=607,
    serialized_end=713,
)


_ECUVERSIONREQUEST = _descriptor.Descriptor(
    name="EcuVersionRequest",
    full_name="OtaClient.EcuVersionRequest",
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
    serialized_start=715,
    serialized_end=734,
)


_ECUVERSIONREPLY = _descriptor.Descriptor(
    name="EcuVersionReply",
    full_name="OtaClient.EcuVersionReply",
    filename=None,
    file=DESCRIPTOR,
    containing_type=None,
    create_key=_descriptor._internal_create_key,
    fields=[
        _descriptor.FieldDescriptor(
            name="ecu_info",
            full_name="OtaClient.EcuVersionReply.ecu_info",
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
    serialized_start=736,
    serialized_end=791,
)


_OTAREBOOTREQUEST = _descriptor.Descriptor(
    name="OtaRebootRequest",
    full_name="OtaClient.OtaRebootRequest",
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
    serialized_start=793,
    serialized_end=811,
)


_OTAREBOOTREPLY = _descriptor.Descriptor(
    name="OtaRebootReply",
    full_name="OtaClient.OtaRebootReply",
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
    serialized_start=813,
    serialized_end=829,
)

_ECUUPDATEINFO.fields_by_name["ecu_info"].message_type = _ECUINFO
_OTAUPDATEREQUEST.fields_by_name["ecu_update_info"].message_type = _ECUUPDATEINFO
_OTAUPDATEREPLY.fields_by_name["result"].enum_type = _UPDATERESULTTYPE
_OTAUPDATEREPLY.fields_by_name["ecu_info"].message_type = _ECUINFO
_OTAROLLBACKREQUEST.fields_by_name["ecu_info"].message_type = _ECUINFO
_OTAROLLBACKREPLY.fields_by_name["result"].enum_type = _ROLLBACKRESULTTYPE
_OTAROLLBACKREPLY.fields_by_name["ecu_info"].message_type = _ECUINFO
_ECUSTATUSREPLY.fields_by_name["status"].enum_type = _ECUSTATUSTYPE
_ECUSTATUSREPLY.fields_by_name["boot_status"].enum_type = _BOOTSTATUSTYPE
_ECUVERSIONREPLY.fields_by_name["ecu_info"].message_type = _ECUINFO
DESCRIPTOR.message_types_by_name["EcuInfo"] = _ECUINFO
DESCRIPTOR.message_types_by_name["EcuUpdateInfo"] = _ECUUPDATEINFO
DESCRIPTOR.message_types_by_name["OtaUpdateRequest"] = _OTAUPDATEREQUEST
DESCRIPTOR.message_types_by_name["OtaUpdateReply"] = _OTAUPDATEREPLY
DESCRIPTOR.message_types_by_name["OtaRollbackRequest"] = _OTAROLLBACKREQUEST
DESCRIPTOR.message_types_by_name["OtaRollbackReply"] = _OTAROLLBACKREPLY
DESCRIPTOR.message_types_by_name["EcuStatusRequest"] = _ECUSTATUSREQUEST
DESCRIPTOR.message_types_by_name["EcuStatusReply"] = _ECUSTATUSREPLY
DESCRIPTOR.message_types_by_name["EcuVersionRequest"] = _ECUVERSIONREQUEST
DESCRIPTOR.message_types_by_name["EcuVersionReply"] = _ECUVERSIONREPLY
DESCRIPTOR.message_types_by_name["OtaRebootRequest"] = _OTAREBOOTREQUEST
DESCRIPTOR.message_types_by_name["OtaRebootReply"] = _OTAREBOOTREPLY
DESCRIPTOR.enum_types_by_name["UpdateResultType"] = _UPDATERESULTTYPE
DESCRIPTOR.enum_types_by_name["RollbackResultType"] = _ROLLBACKRESULTTYPE
DESCRIPTOR.enum_types_by_name["EcuStatusType"] = _ECUSTATUSTYPE
DESCRIPTOR.enum_types_by_name["BootStatusType"] = _BOOTSTATUSTYPE
_sym_db.RegisterFileDescriptor(DESCRIPTOR)

EcuInfo = _reflection.GeneratedProtocolMessageType(
    "EcuInfo",
    (_message.Message,),
    {
        "DESCRIPTOR": _ECUINFO,
        "__module__": "otaclient_pb2"
        # @@protoc_insertion_point(class_scope:OtaClient.EcuInfo)
    },
)
_sym_db.RegisterMessage(EcuInfo)

EcuUpdateInfo = _reflection.GeneratedProtocolMessageType(
    "EcuUpdateInfo",
    (_message.Message,),
    {
        "DESCRIPTOR": _ECUUPDATEINFO,
        "__module__": "otaclient_pb2"
        # @@protoc_insertion_point(class_scope:OtaClient.EcuUpdateInfo)
    },
)
_sym_db.RegisterMessage(EcuUpdateInfo)

OtaUpdateRequest = _reflection.GeneratedProtocolMessageType(
    "OtaUpdateRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _OTAUPDATEREQUEST,
        "__module__": "otaclient_pb2"
        # @@protoc_insertion_point(class_scope:OtaClient.OtaUpdateRequest)
    },
)
_sym_db.RegisterMessage(OtaUpdateRequest)

OtaUpdateReply = _reflection.GeneratedProtocolMessageType(
    "OtaUpdateReply",
    (_message.Message,),
    {
        "DESCRIPTOR": _OTAUPDATEREPLY,
        "__module__": "otaclient_pb2"
        # @@protoc_insertion_point(class_scope:OtaClient.OtaUpdateReply)
    },
)
_sym_db.RegisterMessage(OtaUpdateReply)

OtaRollbackRequest = _reflection.GeneratedProtocolMessageType(
    "OtaRollbackRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _OTAROLLBACKREQUEST,
        "__module__": "otaclient_pb2"
        # @@protoc_insertion_point(class_scope:OtaClient.OtaRollbackRequest)
    },
)
_sym_db.RegisterMessage(OtaRollbackRequest)

OtaRollbackReply = _reflection.GeneratedProtocolMessageType(
    "OtaRollbackReply",
    (_message.Message,),
    {
        "DESCRIPTOR": _OTAROLLBACKREPLY,
        "__module__": "otaclient_pb2"
        # @@protoc_insertion_point(class_scope:OtaClient.OtaRollbackReply)
    },
)
_sym_db.RegisterMessage(OtaRollbackReply)

EcuStatusRequest = _reflection.GeneratedProtocolMessageType(
    "EcuStatusRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _ECUSTATUSREQUEST,
        "__module__": "otaclient_pb2"
        # @@protoc_insertion_point(class_scope:OtaClient.EcuStatusRequest)
    },
)
_sym_db.RegisterMessage(EcuStatusRequest)

EcuStatusReply = _reflection.GeneratedProtocolMessageType(
    "EcuStatusReply",
    (_message.Message,),
    {
        "DESCRIPTOR": _ECUSTATUSREPLY,
        "__module__": "otaclient_pb2"
        # @@protoc_insertion_point(class_scope:OtaClient.EcuStatusReply)
    },
)
_sym_db.RegisterMessage(EcuStatusReply)

EcuVersionRequest = _reflection.GeneratedProtocolMessageType(
    "EcuVersionRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _ECUVERSIONREQUEST,
        "__module__": "otaclient_pb2"
        # @@protoc_insertion_point(class_scope:OtaClient.EcuVersionRequest)
    },
)
_sym_db.RegisterMessage(EcuVersionRequest)

EcuVersionReply = _reflection.GeneratedProtocolMessageType(
    "EcuVersionReply",
    (_message.Message,),
    {
        "DESCRIPTOR": _ECUVERSIONREPLY,
        "__module__": "otaclient_pb2"
        # @@protoc_insertion_point(class_scope:OtaClient.EcuVersionReply)
    },
)
_sym_db.RegisterMessage(EcuVersionReply)

OtaRebootRequest = _reflection.GeneratedProtocolMessageType(
    "OtaRebootRequest",
    (_message.Message,),
    {
        "DESCRIPTOR": _OTAREBOOTREQUEST,
        "__module__": "otaclient_pb2"
        # @@protoc_insertion_point(class_scope:OtaClient.OtaRebootRequest)
    },
)
_sym_db.RegisterMessage(OtaRebootRequest)

OtaRebootReply = _reflection.GeneratedProtocolMessageType(
    "OtaRebootReply",
    (_message.Message,),
    {
        "DESCRIPTOR": _OTAREBOOTREPLY,
        "__module__": "otaclient_pb2"
        # @@protoc_insertion_point(class_scope:OtaClient.OtaRebootReply)
    },
)
_sym_db.RegisterMessage(OtaRebootReply)


DESCRIPTOR._options = None

_OTACLIENTSERVICE = _descriptor.ServiceDescriptor(
    name="OtaClientService",
    full_name="OtaClient.OtaClientService",
    file=DESCRIPTOR,
    index=0,
    serialized_options=None,
    create_key=_descriptor._internal_create_key,
    serialized_start=1471,
    serialized_end=1853,
    methods=[
        _descriptor.MethodDescriptor(
            name="OtaUpdate",
            full_name="OtaClient.OtaClientService.OtaUpdate",
            index=0,
            containing_service=None,
            input_type=_OTAUPDATEREQUEST,
            output_type=_OTAUPDATEREPLY,
            serialized_options=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.MethodDescriptor(
            name="OtaRollback",
            full_name="OtaClient.OtaClientService.OtaRollback",
            index=1,
            containing_service=None,
            input_type=_OTAROLLBACKREQUEST,
            output_type=_OTAROLLBACKREPLY,
            serialized_options=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.MethodDescriptor(
            name="OtaReboot",
            full_name="OtaClient.OtaClientService.OtaReboot",
            index=2,
            containing_service=None,
            input_type=_OTAREBOOTREQUEST,
            output_type=_OTAREBOOTREPLY,
            serialized_options=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.MethodDescriptor(
            name="EcuStatus",
            full_name="OtaClient.OtaClientService.EcuStatus",
            index=3,
            containing_service=None,
            input_type=_ECUSTATUSREQUEST,
            output_type=_ECUSTATUSREPLY,
            serialized_options=None,
            create_key=_descriptor._internal_create_key,
        ),
        _descriptor.MethodDescriptor(
            name="EcuVersion",
            full_name="OtaClient.OtaClientService.EcuVersion",
            index=4,
            containing_service=None,
            input_type=_ECUVERSIONREQUEST,
            output_type=_ECUVERSIONREPLY,
            serialized_options=None,
            create_key=_descriptor._internal_create_key,
        ),
    ],
)
_sym_db.RegisterServiceDescriptor(_OTACLIENTSERVICE)

DESCRIPTOR.services_by_name["OtaClientService"] = _OTACLIENTSERVICE

# @@protoc_insertion_point(module_scope)
