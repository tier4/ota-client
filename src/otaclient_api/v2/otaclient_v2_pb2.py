# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: otaclient_api/v2/otaclient_v2.proto
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import symbol_database as _symbol_database
from google.protobuf.internal import builder as _builder
# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


from google.protobuf import duration_pb2 as google_dot_protobuf_dot_duration__pb2


DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(b'\n#otaclient_api/v2/otaclient_v2.proto\x12\x0bOtaClientV2\x1a\x1egoogle/protobuf/duration.proto\"Q\n\x10UpdateRequestEcu\x12\x0e\n\x06\x65\x63u_id\x18\x01 \x01(\t\x12\x0f\n\x07version\x18\x02 \x01(\t\x12\x0b\n\x03url\x18\x03 \x01(\t\x12\x0f\n\x07\x63ookies\x18\x04 \x01(\t\";\n\rUpdateRequest\x12*\n\x03\x65\x63u\x18\x01 \x03(\x0b\x32\x1d.OtaClientV2.UpdateRequestEcu\"^\n\x11UpdateResponseEcu\x12\x0e\n\x06\x65\x63u_id\x18\x01 \x01(\t\x12(\n\x06result\x18\x02 \x01(\x0e\x32\x18.OtaClientV2.FailureType\x12\x0f\n\x07message\x18\x03 \x01(\t\"=\n\x0eUpdateResponse\x12+\n\x03\x65\x63u\x18\x01 \x03(\x0b\x32\x1e.OtaClientV2.UpdateResponseEcu\"$\n\x12RollbackRequestEcu\x12\x0e\n\x06\x65\x63u_id\x18\x01 \x01(\t\"?\n\x0fRollbackRequest\x12,\n\x03\x65\x63u\x18\x01 \x03(\x0b\x32\x1f.OtaClientV2.RollbackRequestEcu\"`\n\x13RollbackResponseEcu\x12\x0e\n\x06\x65\x63u_id\x18\x01 \x01(\t\x12(\n\x06result\x18\x02 \x01(\x0e\x32\x18.OtaClientV2.FailureType\x12\x0f\n\x07message\x18\x03 \x01(\t\"A\n\x10RollbackResponse\x12-\n\x03\x65\x63u\x18\x01 \x03(\x0b\x32 .OtaClientV2.RollbackResponseEcu\"\x0f\n\rStatusRequest\"\xf6\x04\n\x0eStatusProgress\x12/\n\x05phase\x18\x01 \x01(\x0e\x32 .OtaClientV2.StatusProgressPhase\x12\x1b\n\x13total_regular_files\x18\x02 \x01(\x04\x12\x1f\n\x17regular_files_processed\x18\x03 \x01(\x04\x12\x1c\n\x14\x66iles_processed_copy\x18\x04 \x01(\x04\x12\x1c\n\x14\x66iles_processed_link\x18\x05 \x01(\x04\x12 \n\x18\x66iles_processed_download\x18\x06 \x01(\x04\x12 \n\x18\x66ile_size_processed_copy\x18\x07 \x01(\x04\x12 \n\x18\x66ile_size_processed_link\x18\x08 \x01(\x04\x12$\n\x1c\x66ile_size_processed_download\x18\t \x01(\x04\x12\x34\n\x11\x65lapsed_time_copy\x18\n \x01(\x0b\x32\x19.google.protobuf.Duration\x12\x34\n\x11\x65lapsed_time_link\x18\x0b \x01(\x0b\x32\x19.google.protobuf.Duration\x12\x38\n\x15\x65lapsed_time_download\x18\x0c \x01(\x0b\x32\x19.google.protobuf.Duration\x12\x17\n\x0f\x65rrors_download\x18\r \x01(\x04\x12\x1f\n\x17total_regular_file_size\x18\x0e \x01(\x04\x12\x35\n\x12total_elapsed_time\x18\x0f \x01(\x0b\x32\x19.google.protobuf.Duration\x12\x16\n\x0e\x64ownload_bytes\x18\x10 \x01(\x04\"\xb3\x01\n\x06Status\x12&\n\x06status\x18\x01 \x01(\x0e\x32\x16.OtaClientV2.StatusOta\x12)\n\x07\x66\x61ilure\x18\x02 \x01(\x0e\x32\x18.OtaClientV2.FailureType\x12\x16\n\x0e\x66\x61ilure_reason\x18\x03 \x01(\t\x12\x0f\n\x07version\x18\x04 \x01(\t\x12-\n\x08progress\x18\x05 \x01(\x0b\x32\x1b.OtaClientV2.StatusProgress\"r\n\x11StatusResponseEcu\x12\x0e\n\x06\x65\x63u_id\x18\x01 \x01(\t\x12(\n\x06result\x18\x02 \x01(\x0e\x32\x18.OtaClientV2.FailureType\x12#\n\x06status\x18\x03 \x01(\x0b\x32\x13.OtaClientV2.Status\"\x8e\x01\n\x0eStatusResponse\x12/\n\x03\x65\x63u\x18\x01 \x03(\x0b\x32\x1e.OtaClientV2.StatusResponseEcuB\x02\x18\x01\x12\x19\n\x11\x61vailable_ecu_ids\x18\x02 \x03(\t\x12\x30\n\x06\x65\x63u_v2\x18\x03 \x03(\x0b\x32 .OtaClientV2.StatusResponseEcuV2\"\x87\x03\n\x13StatusResponseEcuV2\x12\x0e\n\x06\x65\x63u_id\x18\x01 \x01(\t\x12\x18\n\x10\x66irmware_version\x18\x02 \x01(\t\x12\x19\n\x11otaclient_version\x18\x03 \x01(\t\x12*\n\nota_status\x18\x0b \x01(\x0e\x32\x16.OtaClientV2.StatusOta\x12\x33\n\x0c\x66\x61ilure_type\x18\x0c \x01(\x0e\x32\x18.OtaClientV2.FailureTypeH\x00\x88\x01\x01\x12\x1b\n\x0e\x66\x61ilure_reason\x18\r \x01(\tH\x01\x88\x01\x01\x12\x1e\n\x11\x66\x61ilure_traceback\x18\x0e \x01(\tH\x02\x88\x01\x01\x12\x35\n\rupdate_status\x18\x0f \x01(\x0b\x32\x19.OtaClientV2.UpdateStatusH\x03\x88\x01\x01\x42\x0f\n\r_failure_typeB\x11\n\x0f_failure_reasonB\x14\n\x12_failure_tracebackB\x10\n\x0e_update_statusJ\x04\x08\x04\x10\x0bJ\x04\x08\x10\x10\x11\"\xdd\x05\n\x0cUpdateStatus\x12\x1f\n\x17update_firmware_version\x18\x01 \x01(\t\x12%\n\x1dtotal_files_size_uncompressed\x18\x02 \x01(\x04\x12\x17\n\x0ftotal_files_num\x18\x03 \x01(\x04\x12\x1e\n\x16update_start_timestamp\x18\x04 \x01(\x04\x12\'\n\x05phase\x18\x0b \x01(\x0e\x32\x18.OtaClientV2.UpdatePhase\x12 \n\x18total_download_files_num\x18\x0c \x01(\x04\x12!\n\x19total_download_files_size\x18\r \x01(\x04\x12\x1c\n\x14\x64ownloaded_files_num\x18\x0e \x01(\x04\x12\x18\n\x10\x64ownloaded_bytes\x18\x0f \x01(\x04\x12\x1d\n\x15\x64ownloaded_files_size\x18\x10 \x01(\x04\x12\x1a\n\x12\x64ownloading_errors\x18\x11 \x01(\x04\x12\x1e\n\x16total_remove_files_num\x18\x12 \x01(\x04\x12\x19\n\x11removed_files_num\x18\x13 \x01(\x04\x12\x1b\n\x13processed_files_num\x18\x14 \x01(\x04\x12\x1c\n\x14processed_files_size\x18\x15 \x01(\x04\x12\x35\n\x12total_elapsed_time\x18\x1f \x01(\x0b\x32\x19.google.protobuf.Duration\x12@\n\x1d\x64\x65lta_generating_elapsed_time\x18  \x01(\x0b\x32\x19.google.protobuf.Duration\x12;\n\x18\x64ownloading_elapsed_time\x18! \x01(\x0b\x32\x19.google.protobuf.Duration\x12?\n\x1cupdate_applying_elapsed_time\x18\" \x01(\x0b\x32\x19.google.protobuf.Duration*A\n\x0b\x46\x61ilureType\x12\x0e\n\nNO_FAILURE\x10\x00\x12\x0f\n\x0bRECOVERABLE\x10\x01\x12\x11\n\rUNRECOVERABLE\x10\x02*\x80\x01\n\tStatusOta\x12\x0f\n\x0bINITIALIZED\x10\x00\x12\x0b\n\x07SUCCESS\x10\x01\x12\x0b\n\x07\x46\x41ILURE\x10\x02\x12\x0c\n\x08UPDATING\x10\x03\x12\x0f\n\x0bROLLBACKING\x10\x04\x12\x14\n\x10ROLLBACK_FAILURE\x10\x05\x12\x13\n\x0f\x43LIENT_UPDATING\x10\x06*~\n\x13StatusProgressPhase\x12\x0b\n\x07INITIAL\x10\x00\x12\x0c\n\x08METADATA\x10\x01\x12\r\n\tDIRECTORY\x10\x02\x12\x0b\n\x07SYMLINK\x10\x03\x12\x0b\n\x07REGULAR\x10\x04\x12\x0e\n\nPERSISTENT\x10\x05\x12\x13\n\x0fPOST_PROCESSING\x10\x06*\xcd\x01\n\x0bUpdatePhase\x12\x10\n\x0cINITIALIZING\x10\x00\x12\x17\n\x13PROCESSING_METADATA\x10\x01\x12\x15\n\x11\x43\x41LCULATING_DELTA\x10\x02\x12\x19\n\x15\x44OWNLOADING_OTA_FILES\x10\x03\x12\x13\n\x0f\x41PPLYING_UPDATE\x10\x04\x12\x19\n\x15PROCESSING_POSTUPDATE\x10\x05\x12\x15\n\x11\x46INALIZING_UPDATE\x10\x06\x12\x1a\n\x16\x44OWNLOADING_OTA_CLIENT\x10\x07\x32\xb2\x02\n\x10OtaClientService\x12\x43\n\x06Update\x12\x1a.OtaClientV2.UpdateRequest\x1a\x1b.OtaClientV2.UpdateResponse\"\x00\x12I\n\x08Rollback\x12\x1c.OtaClientV2.RollbackRequest\x1a\x1d.OtaClientV2.RollbackResponse\"\x00\x12\x43\n\x06Status\x12\x1a.OtaClientV2.StatusRequest\x1a\x1b.OtaClientV2.StatusResponse\"\x00\x12I\n\x0c\x43lientUpdate\x12\x1a.OtaClientV2.UpdateRequest\x1a\x1b.OtaClientV2.UpdateResponse\"\x00\x42\x06\xa2\x02\x03OTAb\x06proto3')

_globals = globals()
_builder.BuildMessageAndEnumDescriptors(DESCRIPTOR, _globals)
_builder.BuildTopDescriptorsAndMessages(DESCRIPTOR, 'otaclient_api.v2.otaclient_v2_pb2', _globals)
if _descriptor._USE_C_DESCRIPTORS == False:

  DESCRIPTOR._options = None
  DESCRIPTOR._serialized_options = b'\242\002\003OTA'
  _STATUSRESPONSE.fields_by_name['ecu']._options = None
  _STATUSRESPONSE.fields_by_name['ecu']._serialized_options = b'\030\001'
  _globals['_FAILURETYPE']._serialized_start=2878
  _globals['_FAILURETYPE']._serialized_end=2943
  _globals['_STATUSOTA']._serialized_start=2946
  _globals['_STATUSOTA']._serialized_end=3074
  _globals['_STATUSPROGRESSPHASE']._serialized_start=3076
  _globals['_STATUSPROGRESSPHASE']._serialized_end=3202
  _globals['_UPDATEPHASE']._serialized_start=3205
  _globals['_UPDATEPHASE']._serialized_end=3410
  _globals['_UPDATEREQUESTECU']._serialized_start=84
  _globals['_UPDATEREQUESTECU']._serialized_end=165
  _globals['_UPDATEREQUEST']._serialized_start=167
  _globals['_UPDATEREQUEST']._serialized_end=226
  _globals['_UPDATERESPONSEECU']._serialized_start=228
  _globals['_UPDATERESPONSEECU']._serialized_end=322
  _globals['_UPDATERESPONSE']._serialized_start=324
  _globals['_UPDATERESPONSE']._serialized_end=385
  _globals['_ROLLBACKREQUESTECU']._serialized_start=387
  _globals['_ROLLBACKREQUESTECU']._serialized_end=423
  _globals['_ROLLBACKREQUEST']._serialized_start=425
  _globals['_ROLLBACKREQUEST']._serialized_end=488
  _globals['_ROLLBACKRESPONSEECU']._serialized_start=490
  _globals['_ROLLBACKRESPONSEECU']._serialized_end=586
  _globals['_ROLLBACKRESPONSE']._serialized_start=588
  _globals['_ROLLBACKRESPONSE']._serialized_end=653
  _globals['_STATUSREQUEST']._serialized_start=655
  _globals['_STATUSREQUEST']._serialized_end=670
  _globals['_STATUSPROGRESS']._serialized_start=673
  _globals['_STATUSPROGRESS']._serialized_end=1303
  _globals['_STATUS']._serialized_start=1306
  _globals['_STATUS']._serialized_end=1485
  _globals['_STATUSRESPONSEECU']._serialized_start=1487
  _globals['_STATUSRESPONSEECU']._serialized_end=1601
  _globals['_STATUSRESPONSE']._serialized_start=1604
  _globals['_STATUSRESPONSE']._serialized_end=1746
  _globals['_STATUSRESPONSEECUV2']._serialized_start=1749
  _globals['_STATUSRESPONSEECUV2']._serialized_end=2140
  _globals['_UPDATESTATUS']._serialized_start=2143
  _globals['_UPDATESTATUS']._serialized_end=2876
  _globals['_OTACLIENTSERVICE']._serialized_start=3413
  _globals['_OTACLIENTSERVICE']._serialized_end=3719
# @@protoc_insertion_point(module_scope)
