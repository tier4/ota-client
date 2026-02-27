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
"""Compatibility tests for otaclient_pb2 version upgrades.

These tests pin the expected symbols, enum integer values, message fields, gRPC
service definitions, and api_types round-trips that the rest of the codebase
depends on. A failure here indicates a breaking change was introduced in an
otaclient_pb2 version upgrade.

Test classes:
- TestPb2SymbolsExist: all required symbols must be importable
- TestEnumValueCompatibility: enum integer values must not change (wire format)
- TestMessageFieldsCompatibility: all fields must be settable/readable by name
- TestMessageSerializeRoundTrip: serialize→deserialize is lossless (field numbers)
- TestApiTypesRoundTrip: api_types.convert() + export_pb() works for every type
"""

from __future__ import annotations

import pytest
from google.protobuf.duration_pb2 import Duration as _Duration
from otaclient_pb2.v2 import otaclient_v2_pb2 as v2
from otaclient_pb2.v2 import otaclient_v2_pb2_grpc as v2_grpc

from otaclient_api.v2 import _types as api_types
from tests.utils import compare_message

# ---------------------------------------------------------------------------
# Symbol existence
# ---------------------------------------------------------------------------


class TestPb2SymbolsExist:
    """All symbols referenced by otaclient_api must be importable from otaclient_pb2."""

    def test_failure_type_enum_symbols(self):
        assert hasattr(v2, "NO_FAILURE")
        assert hasattr(v2, "RECOVERABLE")
        assert hasattr(v2, "UNRECOVERABLE")

    def test_abort_failure_type_enum_symbols(self):
        assert hasattr(v2, "ABORT_NO_FAILURE")
        assert hasattr(v2, "ABORT_FAILURE")

    def test_status_ota_enum_symbols(self):
        for name in (
            "INITIALIZED",
            "SUCCESS",
            "FAILURE",
            "UPDATING",
            "ROLLBACKING",
            "ROLLBACK_FAILURE",
            "CLIENT_UPDATING",
            "ABORTING",
            "ABORTED",
        ):
            assert hasattr(v2, name), f"missing StatusOta symbol: {name}"

    def test_status_progress_phase_enum_symbols(self):
        for name in (
            "INITIAL",
            "METADATA",
            "DIRECTORY",
            "SYMLINK",
            "REGULAR",
            "PERSISTENT",
            "POST_PROCESSING",
        ):
            assert hasattr(v2, name), f"missing StatusProgressPhase symbol: {name}"

    def test_update_phase_enum_symbols(self):
        for name in (
            "INITIALIZING",
            "PROCESSING_METADATA",
            "CALCULATING_DELTA",
            "DOWNLOADING_OTA_FILES",
            "APPLYING_UPDATE",
            "PROCESSING_POSTUPDATE",
            "FINALIZING_UPDATE",
            "DOWNLOADING_OTA_CLIENT",
        ):
            assert hasattr(v2, name), f"missing UpdatePhase symbol: {name}"

    def test_message_class_symbols(self):
        for name in (
            # update
            "UpdateRequestEcu",
            "UpdateRequest",
            "UpdateResponseEcu",
            "UpdateResponse",
            # abort
            "AbortRequestEcu",
            "AbortRequest",
            "AbortResponseEcu",
            "AbortResponse",
            # rollback
            "RollbackRequestEcu",
            "RollbackRequest",
            "RollbackResponseEcu",
            "RollbackResponse",
            # status
            "StatusRequest",
            "StatusProgress",
            "Status",
            "StatusResponseEcu",
            "StatusResponse",
            "StatusResponseEcuV2",
            "UpdateStatus",
        ):
            assert hasattr(v2, name), f"missing message class: {name}"

    def test_grpc_class_symbols(self):
        assert hasattr(v2_grpc, "OtaClientServiceStub")
        assert hasattr(v2_grpc, "OtaClientServiceServicer")
        assert hasattr(v2_grpc, "add_OtaClientServiceServicer_to_server")

    def test_grpc_servicer_rpc_methods(self):
        servicer = v2_grpc.OtaClientServiceServicer()
        for method in ("Update", "Abort", "Rollback", "ClientUpdate", "Status"):
            assert hasattr(servicer, method), f"missing RPC method: {method}"


# ---------------------------------------------------------------------------
# Enum value compatibility (wire format)
# ---------------------------------------------------------------------------


class TestEnumValueCompatibility:
    """Enum integer values must not change between otaclient_pb2 versions.

    Changing an enum integer value corrupts existing serialized protobuf messages.
    These expected values are derived directly from otaclient_v2.proto.
    """

    @pytest.mark.parametrize(
        "name, expected",
        [
            ("NO_FAILURE", 0),
            ("RECOVERABLE", 1),
            ("UNRECOVERABLE", 2),
        ],
    )
    def test_failure_type(self, name, expected):
        assert getattr(v2, name) == expected

    @pytest.mark.parametrize(
        "name, expected",
        [
            ("ABORT_NO_FAILURE", 0),
            ("ABORT_FAILURE", 1),
        ],
    )
    def test_abort_failure_type(self, name, expected):
        assert getattr(v2, name) == expected

    @pytest.mark.parametrize(
        "name, expected",
        [
            ("INITIALIZED", 0),
            ("SUCCESS", 1),
            ("FAILURE", 2),
            ("UPDATING", 3),
            ("ROLLBACKING", 4),
            ("ROLLBACK_FAILURE", 5),
            ("CLIENT_UPDATING", 6),
            ("ABORTING", 7),
            ("ABORTED", 8),
        ],
    )
    def test_status_ota(self, name, expected):
        assert getattr(v2, name) == expected

    @pytest.mark.parametrize(
        "name, expected",
        [
            ("INITIAL", 0),
            ("METADATA", 1),
            ("DIRECTORY", 2),
            ("SYMLINK", 3),
            ("REGULAR", 4),
            ("PERSISTENT", 5),
            ("POST_PROCESSING", 6),
        ],
    )
    def test_status_progress_phase(self, name, expected):
        assert getattr(v2, name) == expected

    @pytest.mark.parametrize(
        "name, expected",
        [
            ("INITIALIZING", 0),
            ("PROCESSING_METADATA", 1),
            ("CALCULATING_DELTA", 2),
            ("DOWNLOADING_OTA_FILES", 3),
            ("APPLYING_UPDATE", 4),
            ("PROCESSING_POSTUPDATE", 5),
            ("FINALIZING_UPDATE", 6),
            ("DOWNLOADING_OTA_CLIENT", 7),
        ],
    )
    def test_update_phase(self, name, expected):
        assert getattr(v2, name) == expected


# ---------------------------------------------------------------------------
# Message field compatibility
# ---------------------------------------------------------------------------


class TestMessageFieldsCompatibility:
    """All message fields must be settable and readable by name.

    A failure means a field was renamed or removed in otaclient_pb2.
    """

    def test_update_request_ecu(self):
        msg = v2.UpdateRequestEcu(
            ecu_id="ecu_1",
            version="1.0.0",
            url="http://example.com/ota",
            cookies='{"key":"value"}',
        )
        assert msg.ecu_id == "ecu_1"
        assert msg.version == "1.0.0"
        assert msg.url == "http://example.com/ota"
        assert msg.cookies == '{"key":"value"}'

    def test_update_request(self):
        msg = v2.UpdateRequest(
            ecu=[v2.UpdateRequestEcu(ecu_id="ecu_1")],
            request_id="req-001",
        )
        assert len(msg.ecu) == 1
        assert msg.request_id == "req-001"

    def test_update_response_ecu(self):
        msg = v2.UpdateResponseEcu(
            ecu_id="ecu_1",
            result=v2.NO_FAILURE,
            message="ok",
        )
        assert msg.ecu_id == "ecu_1"
        assert msg.result == v2.NO_FAILURE
        assert msg.message == "ok"

    def test_update_response(self):
        msg = v2.UpdateResponse(
            ecu=[v2.UpdateResponseEcu(ecu_id="ecu_1", result=v2.NO_FAILURE)],
        )
        assert len(msg.ecu) == 1

    def test_abort_request_ecu(self):
        msg = v2.AbortRequestEcu(ecu_id="ecu_1")
        assert msg.ecu_id == "ecu_1"

    def test_abort_request(self):
        msg = v2.AbortRequest(
            ecu=[v2.AbortRequestEcu(ecu_id="ecu_1")],
            request_id="req-002",
        )
        assert len(msg.ecu) == 1
        assert msg.request_id == "req-002"

    def test_abort_response_ecu(self):
        msg = v2.AbortResponseEcu(
            ecu_id="ecu_1",
            result=v2.ABORT_NO_FAILURE,
            message="aborted",
        )
        assert msg.ecu_id == "ecu_1"
        assert msg.result == v2.ABORT_NO_FAILURE
        assert msg.message == "aborted"

    def test_abort_response(self):
        msg = v2.AbortResponse(
            ecu=[v2.AbortResponseEcu(ecu_id="ecu_1", result=v2.ABORT_NO_FAILURE)],
        )
        assert len(msg.ecu) == 1

    def test_rollback_request_ecu(self):
        msg = v2.RollbackRequestEcu(ecu_id="ecu_1")
        assert msg.ecu_id == "ecu_1"

    def test_rollback_request(self):
        msg = v2.RollbackRequest(
            ecu=[v2.RollbackRequestEcu(ecu_id="ecu_1")],
            request_id="req-003",
        )
        assert len(msg.ecu) == 1
        assert msg.request_id == "req-003"

    def test_rollback_response_ecu(self):
        msg = v2.RollbackResponseEcu(
            ecu_id="ecu_1",
            result=v2.NO_FAILURE,
            message="ok",
        )
        assert msg.ecu_id == "ecu_1"
        assert msg.result == v2.NO_FAILURE
        assert msg.message == "ok"

    def test_rollback_response(self):
        msg = v2.RollbackResponse(
            ecu=[v2.RollbackResponseEcu(ecu_id="ecu_1", result=v2.NO_FAILURE)],
        )
        assert len(msg.ecu) == 1

    def test_status_request(self):
        msg = v2.StatusRequest()
        assert msg is not None

    def test_status_progress_all_fields(self):
        msg = v2.StatusProgress(
            phase=v2.REGULAR,
            total_regular_files=270265,
            regular_files_processed=270258,
            files_processed_copy=44315,
            files_processed_link=144,
            files_processed_download=225799,
            file_size_processed_copy=853481625,
            file_size_processed_link=427019589,
            file_size_processed_download=24457642270,
            elapsed_time_copy=_Duration(seconds=15, nanos=49000000),
            elapsed_time_link=_Duration(seconds=1),
            elapsed_time_download=_Duration(seconds=416, nanos=945000000),
            errors_download=3,
            total_regular_file_size=25740860425,
            total_elapsed_time=_Duration(seconds=512, nanos=8000000),
            download_bytes=24457642270,
        )
        assert msg.phase == v2.REGULAR
        assert msg.total_regular_files == 270265
        assert msg.regular_files_processed == 270258
        assert msg.files_processed_copy == 44315
        assert msg.files_processed_link == 144
        assert msg.files_processed_download == 225799
        assert msg.file_size_processed_copy == 853481625
        assert msg.file_size_processed_link == 427019589
        assert msg.file_size_processed_download == 24457642270
        assert msg.elapsed_time_copy == _Duration(seconds=15, nanos=49000000)
        assert msg.elapsed_time_link == _Duration(seconds=1)
        assert msg.elapsed_time_download == _Duration(seconds=416, nanos=945000000)
        assert msg.errors_download == 3
        assert msg.total_regular_file_size == 25740860425
        assert msg.total_elapsed_time == _Duration(seconds=512, nanos=8000000)
        assert msg.download_bytes == 24457642270

    def test_status_all_fields(self):
        msg = v2.Status(
            status=v2.UPDATING,
            failure=v2.NO_FAILURE,
            failure_reason="some reason",
            version="1.0.0",
            progress=v2.StatusProgress(phase=v2.REGULAR, total_regular_files=100),
        )
        assert msg.status == v2.UPDATING
        assert msg.failure == v2.NO_FAILURE
        assert msg.failure_reason == "some reason"
        assert msg.version == "1.0.0"
        assert msg.progress.phase == v2.REGULAR

    def test_status_response_ecu(self):
        msg = v2.StatusResponseEcu(
            ecu_id="ecu_1",
            result=v2.NO_FAILURE,
            status=v2.Status(status=v2.SUCCESS, version="1.0.0"),
        )
        assert msg.ecu_id == "ecu_1"
        assert msg.result == v2.NO_FAILURE
        assert msg.status.status == v2.SUCCESS

    def test_update_status_all_fields(self):
        msg = v2.UpdateStatus(
            update_firmware_version="2.0.0",
            total_files_size_uncompressed=25740860425,
            total_files_num=270265,
            update_start_timestamp=1700000000,
            phase=v2.DOWNLOADING_OTA_FILES,
            total_download_files_num=225799,
            total_download_files_size=24457642270,
            downloaded_files_num=100000,
            downloaded_bytes=10000000,
            downloaded_files_size=10000000,
            downloading_errors=2,
            total_remove_files_num=5000,
            removed_files_num=2500,
            processed_files_num=150000,
            processed_files_size=15000000,
            total_elapsed_time=_Duration(seconds=512),
            delta_generating_elapsed_time=_Duration(seconds=5),
            downloading_elapsed_time=_Duration(seconds=300),
            update_applying_elapsed_time=_Duration(seconds=200),
        )
        assert msg.update_firmware_version == "2.0.0"
        assert msg.total_files_num == 270265
        assert msg.update_start_timestamp == 1700000000
        assert msg.phase == v2.DOWNLOADING_OTA_FILES
        assert msg.total_download_files_num == 225799
        assert msg.downloaded_files_num == 100000
        assert msg.downloaded_bytes == 10000000
        assert msg.downloading_errors == 2
        assert msg.total_remove_files_num == 5000
        assert msg.removed_files_num == 2500
        assert msg.processed_files_num == 150000
        assert msg.processed_files_size == 15000000
        assert msg.total_elapsed_time == _Duration(seconds=512)
        assert msg.delta_generating_elapsed_time == _Duration(seconds=5)
        assert msg.downloading_elapsed_time == _Duration(seconds=300)
        assert msg.update_applying_elapsed_time == _Duration(seconds=200)

    def test_status_response_ecu_v2_all_fields(self):
        msg = v2.StatusResponseEcuV2(
            ecu_id="ecu_1",
            firmware_version="1.0.0",
            otaclient_version="2.5.0",
            ota_status=v2.UPDATING,
            failure_type=v2.NO_FAILURE,
            failure_reason="",
            failure_traceback="",
            update_status=v2.UpdateStatus(
                phase=v2.DOWNLOADING_OTA_FILES,
                total_files_num=100,
            ),
        )
        assert msg.ecu_id == "ecu_1"
        assert msg.firmware_version == "1.0.0"
        assert msg.otaclient_version == "2.5.0"
        assert msg.ota_status == v2.UPDATING
        assert msg.update_status.phase == v2.DOWNLOADING_OTA_FILES

    def test_status_response_all_fields(self):
        msg = v2.StatusResponse(
            ecu=[v2.StatusResponseEcu(ecu_id="ecu_1", result=v2.NO_FAILURE)],
            available_ecu_ids=["ecu_1", "ecu_2"],
            ecu_v2=[
                v2.StatusResponseEcuV2(ecu_id="ecu_1", ota_status=v2.SUCCESS),
            ],
        )
        assert len(msg.ecu) == 1
        assert list(msg.available_ecu_ids) == ["ecu_1", "ecu_2"]
        assert len(msg.ecu_v2) == 1


# ---------------------------------------------------------------------------
# Serialize/deserialize round-trips (validates field numbers)
# ---------------------------------------------------------------------------


class TestMessageSerializeRoundTrip:
    """Messages must survive SerializeToString → ParseFromString losslessly.

    This implicitly validates that field numbers have not changed, because
    protobuf encodes field identities as field numbers in the wire format.
    """

    @staticmethod
    def _rt(msg):
        data = msg.SerializeToString()
        restored = type(msg)()
        restored.ParseFromString(data)
        assert restored == msg
        return restored

    def test_update_request_ecu(self):
        self._rt(
            v2.UpdateRequestEcu(
                ecu_id="ecu_1", version="1.0", url="http://example.com", cookies="{}"
            )
        )

    def test_update_request(self):
        self._rt(
            v2.UpdateRequest(
                ecu=[
                    v2.UpdateRequestEcu(
                        ecu_id="ecu_1", version="1.0", url="http://example.com"
                    )
                ],
                request_id="req-001",
            )
        )

    def test_update_response(self):
        self._rt(
            v2.UpdateResponse(
                ecu=[
                    v2.UpdateResponseEcu(
                        ecu_id="ecu_1", result=v2.NO_FAILURE, message="ok"
                    )
                ]
            )
        )

    def test_abort_request(self):
        self._rt(
            v2.AbortRequest(
                ecu=[v2.AbortRequestEcu(ecu_id="ecu_1")],
                request_id="req-002",
            )
        )

    def test_abort_response(self):
        self._rt(
            v2.AbortResponse(
                ecu=[
                    v2.AbortResponseEcu(
                        ecu_id="ecu_1",
                        result=v2.ABORT_NO_FAILURE,
                        message="aborted",
                    )
                ]
            )
        )

    def test_rollback_request(self):
        self._rt(
            v2.RollbackRequest(
                ecu=[v2.RollbackRequestEcu(ecu_id="ecu_1")],
                request_id="req-003",
            )
        )

    def test_rollback_response(self):
        self._rt(
            v2.RollbackResponse(
                ecu=[
                    v2.RollbackResponseEcu(
                        ecu_id="ecu_1", result=v2.NO_FAILURE, message="ok"
                    )
                ]
            )
        )

    def test_status_progress(self):
        self._rt(
            v2.StatusProgress(
                phase=v2.REGULAR,
                total_regular_files=270265,
                regular_files_processed=270258,
                files_processed_copy=44315,
                files_processed_link=144,
                files_processed_download=225799,
                file_size_processed_copy=853481625,
                file_size_processed_link=427019589,
                file_size_processed_download=24457642270,
                elapsed_time_copy=_Duration(seconds=15, nanos=49000000),
                elapsed_time_download=_Duration(seconds=416, nanos=945000000),
                errors_download=0,
                total_regular_file_size=25740860425,
                total_elapsed_time=_Duration(seconds=512, nanos=8000000),
                download_bytes=24457642270,
            )
        )

    def test_status(self):
        self._rt(
            v2.Status(
                status=v2.UPDATING,
                failure=v2.NO_FAILURE,
                failure_reason="",
                version="1.0.0",
                progress=v2.StatusProgress(
                    phase=v2.REGULAR,
                    total_regular_files=100,
                ),
            )
        )

    def test_update_status(self):
        self._rt(
            v2.UpdateStatus(
                update_firmware_version="2.0.0",
                total_files_num=270265,
                update_start_timestamp=1700000000,
                phase=v2.DOWNLOADING_OTA_FILES,
                total_download_files_num=225799,
                downloaded_files_num=100000,
                downloaded_bytes=10000000,
                downloading_errors=0,
                processed_files_num=150000,
                total_elapsed_time=_Duration(seconds=512),
                downloading_elapsed_time=_Duration(seconds=300),
            )
        )

    def test_status_response_ecu_v2(self):
        self._rt(
            v2.StatusResponseEcuV2(
                ecu_id="ecu_1",
                firmware_version="1.0.0",
                otaclient_version="2.5.0",
                ota_status=v2.UPDATING,
                failure_type=v2.NO_FAILURE,
                update_status=v2.UpdateStatus(
                    phase=v2.DOWNLOADING_OTA_FILES,
                    total_files_num=100,
                ),
            )
        )

    def test_status_response(self):
        self._rt(
            v2.StatusResponse(
                ecu=[
                    v2.StatusResponseEcu(
                        ecu_id="ecu_1",
                        result=v2.NO_FAILURE,
                        status=v2.Status(status=v2.UPDATING, version="1.0.0"),
                    )
                ],
                available_ecu_ids=["ecu_1", "ecu_2"],
                ecu_v2=[
                    v2.StatusResponseEcuV2(
                        ecu_id="ecu_1",
                        ota_status=v2.UPDATING,
                        update_status=v2.UpdateStatus(phase=v2.DOWNLOADING_OTA_FILES),
                    )
                ],
            )
        )


# ---------------------------------------------------------------------------
# api_types convert / export_pb round-trips
# ---------------------------------------------------------------------------


class TestApiTypesRoundTrip:
    """api_types.MessageWrapper.convert() and export_pb() must work for every type.

    Pattern: create a pb2 message → convert to api_types wrapper → export_pb()
    back to pb2 → compare original vs exported.

    A failure indicates that otaclient_api._types no longer matches the pb2 schema
    (e.g. a field referenced in _types no longer exists in the new pb2 version).
    """

    # ------ Update API ------

    def test_update_request_ecu(self):
        origin = v2.UpdateRequestEcu(
            ecu_id="ecu_1", version="1.0.0", url="http://example.com", cookies="{}"
        )
        compare_message(origin, api_types.UpdateRequestEcu.convert(origin).export_pb())

    def test_update_request(self):
        origin = v2.UpdateRequest(
            ecu=[
                v2.UpdateRequestEcu(
                    ecu_id="ecu_1",
                    version="1.0.0",
                    url="http://example.com",
                    cookies="{}",
                ),
                v2.UpdateRequestEcu(
                    ecu_id="ecu_2",
                    version="1.0.0",
                    url="http://example.com",
                    cookies="{}",
                ),
            ],
            request_id="req-001",
        )
        compare_message(origin, api_types.UpdateRequest.convert(origin).export_pb())

    def test_update_response_ecu(self):
        origin = v2.UpdateResponseEcu(
            ecu_id="ecu_1", result=v2.NO_FAILURE, message="ok"
        )
        compare_message(origin, api_types.UpdateResponseEcu.convert(origin).export_pb())

    def test_update_response(self):
        origin = v2.UpdateResponse(
            ecu=[
                v2.UpdateResponseEcu(ecu_id="ecu_1", result=v2.NO_FAILURE),
                v2.UpdateResponseEcu(ecu_id="ecu_2", result=v2.RECOVERABLE),
            ]
        )
        compare_message(origin, api_types.UpdateResponse.convert(origin).export_pb())

    # ------ ClientUpdate API (shares pb2 types with Update) ------

    def test_client_update_request(self):
        origin = v2.UpdateRequest(
            ecu=[
                v2.UpdateRequestEcu(
                    ecu_id="ecu_1", version="2.0.0", url="http://example.com"
                )
            ],
            request_id="req-cu-001",
        )
        compare_message(
            origin, api_types.ClientUpdateRequest.convert(origin).export_pb()
        )

    def test_client_update_response(self):
        origin = v2.UpdateResponse(
            ecu=[v2.UpdateResponseEcu(ecu_id="ecu_1", result=v2.NO_FAILURE)]
        )
        compare_message(
            origin, api_types.ClientUpdateResponse.convert(origin).export_pb()
        )

    # ------ Abort API ------

    def test_abort_request_ecu(self):
        origin = v2.AbortRequestEcu(ecu_id="ecu_1")
        compare_message(origin, api_types.AbortRequestEcu.convert(origin).export_pb())

    def test_abort_request(self):
        origin = v2.AbortRequest(
            ecu=[
                v2.AbortRequestEcu(ecu_id="ecu_1"),
                v2.AbortRequestEcu(ecu_id="ecu_2"),
            ],
            request_id="req-abort-001",
        )
        compare_message(origin, api_types.AbortRequest.convert(origin).export_pb())

    def test_abort_response_ecu(self):
        origin = v2.AbortResponseEcu(
            ecu_id="ecu_1", result=v2.ABORT_NO_FAILURE, message="aborted"
        )
        compare_message(origin, api_types.AbortResponseEcu.convert(origin).export_pb())

    def test_abort_response(self):
        origin = v2.AbortResponse(
            ecu=[
                v2.AbortResponseEcu(ecu_id="ecu_1", result=v2.ABORT_NO_FAILURE),
                v2.AbortResponseEcu(ecu_id="ecu_2", result=v2.ABORT_FAILURE),
            ]
        )
        compare_message(origin, api_types.AbortResponse.convert(origin).export_pb())

    # ------ Rollback API ------

    def test_rollback_request_ecu(self):
        origin = v2.RollbackRequestEcu(ecu_id="ecu_1")
        compare_message(
            origin, api_types.RollbackRequestEcu.convert(origin).export_pb()
        )

    def test_rollback_request(self):
        origin = v2.RollbackRequest(
            ecu=[
                v2.RollbackRequestEcu(ecu_id="ecu_1"),
                v2.RollbackRequestEcu(ecu_id="ecu_2"),
            ],
            request_id="req-rb-001",
        )
        compare_message(origin, api_types.RollbackRequest.convert(origin).export_pb())

    def test_rollback_response_ecu(self):
        origin = v2.RollbackResponseEcu(
            ecu_id="ecu_1", result=v2.NO_FAILURE, message="ok"
        )
        compare_message(
            origin, api_types.RollbackResponseEcu.convert(origin).export_pb()
        )

    def test_rollback_response(self):
        origin = v2.RollbackResponse(
            ecu=[
                v2.RollbackResponseEcu(ecu_id="ecu_1", result=v2.NO_FAILURE),
                v2.RollbackResponseEcu(ecu_id="ecu_2", result=v2.RECOVERABLE),
            ]
        )
        compare_message(origin, api_types.RollbackResponse.convert(origin).export_pb())

    # ------ Status API ------

    def test_status_request(self):
        origin = v2.StatusRequest()
        compare_message(origin, api_types.StatusRequest.convert(origin).export_pb())

    def test_status_progress(self):
        origin = v2.StatusProgress(
            phase=v2.REGULAR,
            total_regular_files=270265,
            regular_files_processed=270258,
            files_processed_copy=44315,
            files_processed_link=144,
            files_processed_download=225799,
            file_size_processed_copy=853481625,
            file_size_processed_link=427019589,
            file_size_processed_download=24457642270,
            elapsed_time_copy=_Duration(seconds=15, nanos=49000000),
            elapsed_time_download=_Duration(seconds=416, nanos=945000000),
            errors_download=0,
            total_regular_file_size=25740860425,
            total_elapsed_time=_Duration(seconds=512, nanos=8000000),
            download_bytes=24457642270,
        )
        compare_message(origin, api_types.StatusProgress.convert(origin).export_pb())

    def test_status(self):
        origin = v2.Status(
            status=v2.UPDATING,
            failure=v2.NO_FAILURE,
            failure_reason="reason",
            version="1.0.0",
            progress=v2.StatusProgress(
                phase=v2.REGULAR,
                total_regular_files=100,
                elapsed_time_copy=_Duration(seconds=1),
            ),
        )
        compare_message(origin, api_types.Status.convert(origin).export_pb())

    def test_status_response_ecu(self):
        origin = v2.StatusResponseEcu(
            ecu_id="ecu_1",
            result=v2.NO_FAILURE,
            status=v2.Status(
                status=v2.UPDATING,
                failure=v2.NO_FAILURE,
                version="1.0.0",
                progress=v2.StatusProgress(
                    phase=v2.REGULAR,
                    total_regular_files=100,
                    elapsed_time_copy=_Duration(seconds=5),
                ),
            ),
        )
        compare_message(origin, api_types.StatusResponseEcu.convert(origin).export_pb())

    def test_update_status(self):
        origin = v2.UpdateStatus(
            update_firmware_version="2.0.0",
            total_files_size_uncompressed=25740860425,
            total_files_num=270265,
            update_start_timestamp=1700000000,
            phase=v2.DOWNLOADING_OTA_FILES,
            total_download_files_num=225799,
            total_download_files_size=24457642270,
            downloaded_files_num=100000,
            downloaded_bytes=10000000,
            downloaded_files_size=10000000,
            downloading_errors=2,
            total_remove_files_num=5000,
            removed_files_num=2500,
            processed_files_num=150000,
            processed_files_size=15000000,
            total_elapsed_time=_Duration(seconds=512),
            delta_generating_elapsed_time=_Duration(seconds=5),
            downloading_elapsed_time=_Duration(seconds=300),
            update_applying_elapsed_time=_Duration(seconds=200),
        )
        compare_message(origin, api_types.UpdateStatus.convert(origin).export_pb())

    def test_status_response_ecu_v2(self):
        origin = v2.StatusResponseEcuV2(
            ecu_id="ecu_1",
            firmware_version="1.0.0",
            otaclient_version="2.5.0",
            ota_status=v2.UPDATING,
            failure_type=v2.NO_FAILURE,
            failure_reason="",
            failure_traceback="",
            update_status=v2.UpdateStatus(
                update_firmware_version="2.0.0",
                total_files_num=270265,
                phase=v2.DOWNLOADING_OTA_FILES,
                downloaded_files_num=100000,
                total_elapsed_time=_Duration(seconds=512),
                downloading_elapsed_time=_Duration(seconds=300),
            ),
        )
        compare_message(
            origin, api_types.StatusResponseEcuV2.convert(origin).export_pb()
        )

    def test_status_response(self):
        origin = v2.StatusResponse(
            ecu=[
                v2.StatusResponseEcu(
                    ecu_id="ecu_1",
                    result=v2.NO_FAILURE,
                    status=v2.Status(
                        status=v2.UPDATING,
                        version="1.0.0",
                        progress=v2.StatusProgress(
                            phase=v2.REGULAR,
                            total_regular_files=100,
                            elapsed_time_copy=_Duration(seconds=1),
                        ),
                    ),
                ),
                v2.StatusResponseEcu(
                    ecu_id="ecu_2",
                    result=v2.NO_FAILURE,
                    status=v2.Status(status=v2.SUCCESS, version="1.0.0"),
                ),
            ],
            available_ecu_ids=["ecu_1", "ecu_2"],
            ecu_v2=[
                v2.StatusResponseEcuV2(
                    ecu_id="ecu_1",
                    firmware_version="1.0.0",
                    ota_status=v2.UPDATING,
                    update_status=v2.UpdateStatus(
                        phase=v2.DOWNLOADING_OTA_FILES,
                        total_files_num=270265,
                        total_elapsed_time=_Duration(seconds=100),
                    ),
                ),
            ],
        )
        compare_message(origin, api_types.StatusResponse.convert(origin).export_pb())
