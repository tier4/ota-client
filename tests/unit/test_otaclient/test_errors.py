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
"""Tests for OTA error code definitions."""

from __future__ import annotations

import pickle
from typing import Iterator

import pytest

from otaclient import errors as ota_errors
from otaclient._types import (
    OTAClientStatus,
    OTAStatus,
    UpdateMeta,
    UpdateProgress,
    UpdateTiming,
    VersionDetail,
)
from otaclient.errors import FailureCategory, OTAError, OTAErrorCode
from otaclient.main import MAX_TRACEBACK_SIZE, STATUS_SHM_SIZE
from otaclient_common._shm import MPSharedMemoryWriter


def _iter_all_ota_errors(_cls: type[OTAError] = OTAError) -> Iterator[type[OTAError]]:
    for _sub in _cls.__subclasses__():
        yield _sub
        yield from _iter_all_ota_errors(_sub)


ALL_OTA_ERRORS = sorted(set(_iter_all_ota_errors()), key=lambda c: c.__name__)


class TestErrorCodeDefinition:
    def test_each_error_has_its_own_errcode(self):
        _errcode_mapping: dict[OTAErrorCode, type[OTAError]] = {}
        for _err_cls in ALL_OTA_ERRORS:
            _errcode = _err_cls.failure_errcode
            assert _errcode not in _errcode_mapping, (
                f"{_err_cls} and {_errcode_mapping[_errcode]} share {_errcode}"
            )
            _errcode_mapping[_errcode] = _err_cls

    @pytest.mark.parametrize("err_cls", ALL_OTA_ERRORS, ids=lambda c: c.__name__)
    def test_errcode_in_http_like_range(self, err_cls: type[OTAError]):
        """All errors use 4xx(USER) or 5xx(SYSTEM), legacy 1xx/2xx/3xx MUST NOT be used."""
        assert 400 <= err_cls.failure_errcode.value < 600

    @pytest.mark.parametrize(
        "errcode, expected",
        [
            (OTAErrorCode.E_INVALID_OTAUPDATE_REQUEST, FailureCategory.USER),
            (OTAErrorCode.E_OTA_ABORTED, FailureCategory.USER),
            (OTAErrorCode.E_OTA_ERR_UNRECOVERABLE, FailureCategory.SYSTEM),
            (OTAErrorCode.E_BOOTCONTROL_POSTROLLBACK_FAILED, FailureCategory.SYSTEM),
            (OTAErrorCode.E_UNSPECIFIC, FailureCategory.SYSTEM),
        ],
    )
    def test_category_by_errcode_range(
        self, errcode: OTAErrorCode, expected: FailureCategory
    ):
        assert errcode.category == expected

    def test_user_side_errors(self):
        """Lock the USER side errors, any change to this list is an API change."""
        _user_errors = {
            _err_cls.__name__
            for _err_cls in ALL_OTA_ERRORS
            if _err_cls.failure_errcode.category == FailureCategory.USER
        }
        assert _user_errors == {
            "InvalidUpdateRequest",
            "UpdateRequestCookieInvalid",
            "OTAImageNotFound",
            "OTABusy",
            "InvalidStatusForOTARollback",
            "OTAImageInvalid",
            "MetadataJWTVerficationFailed",
            "MetadataJWTInvalid",
            "BootControlBSPVersionCompatibilityFailed",
            "ClientUpdateSameVersions",
            "OTAAbortSignal",
        }

    def test_failure_category_of_instance(self):
        assert (
            ota_errors.OTAImageNotFound(module=__name__).failure_category
            == FailureCategory.USER
        )
        # NOTE: sub-class of a USER side error can still be a SYSTEM side error
        assert (
            ota_errors.CACertNotInstalled(module=__name__).failure_category
            == FailureCategory.SYSTEM
        )


class TestFailureReason:
    def test_without_detail(self):
        _err = ota_errors.ApplyOTAUpdateFailed(module=__name__)
        assert _err.get_failure_reason() == (
            f"E542: {ota_errors.ApplyOTAUpdateFailed.failure_description}"
        )

    def test_with_detail(self):
        _err = ota_errors.DownloadStalled("no progress for 300s", module=__name__)
        assert _err.get_failure_reason() == (
            f"E504: {ota_errors.DownloadStalled.failure_description}; "
            "detail: no progress for 300s"
        )

    def test_detail_is_truncated(self):
        _err = ota_errors.NetworkError("x" * 10_000, module=__name__)
        _detail = _err.failure_detail
        assert len(_detail) == ota_errors.MAX_FAILURE_DETAIL_LEN
        assert _detail.endswith("...")

    def test_status_with_max_failure_info_fits_in_shm(self):
        """The whole status is pickled into a fixed size shm, ensure that the
        failure_reason with detail message doesn't make the status exceed the shm size."""
        _longest_desc_err = max(
            ALL_OTA_ERRORS, key=lambda c: len(c.failure_description)
        )
        _err = _longest_desc_err("x" * 10_000, module=__name__)
        _status = OTAClientStatus(
            firmware_version="v" * 64,
            version_detail=VersionDetail(
                release_name="r" * 64, release_id="i" * 64, image_id="m" * 64
            ),
            ota_status=OTAStatus.FAILURE,
            update_meta=UpdateMeta(),
            update_progress=UpdateProgress(),
            update_timing=UpdateTiming(),
            failure_type=_err.failure_type,
            failure_reason=_err.get_failure_reason(),
            failure_traceback="t" * MAX_TRACEBACK_SIZE,
        )
        assert (
            len(pickle.dumps(_status))
            <= STATUS_SHM_SIZE - MPSharedMemoryWriter.MIN_ENCAP_MSG_LEN
        )


class TestRaiseAsOTAError:
    def test_convert_non_ota_error(self):
        _cause = OSError("mount failed")
        with pytest.raises(ota_errors.BootControlSlotMountFailed) as exc_info:
            with ota_errors.raise_as_ota_error(
                ota_errors.BootControlSlotMountFailed,
                "failed to mount slots",
                module=__name__,
            ):
                raise _cause

        _err = exc_info.value
        assert _err.__cause__ is _cause
        assert _err.module == __name__
        assert "failed to mount slots" in _err.failure_detail
        assert "mount failed" in _err.failure_detail

    def test_ota_error_passthrough(self):
        _inner = ota_errors.StandbySlotInsufficientSpace("ENOSPC", module=__name__)
        with pytest.raises(ota_errors.StandbySlotInsufficientSpace) as exc_info:
            with ota_errors.raise_as_ota_error(
                ota_errors.PostUpdateFailed, "post-update failed", module=__name__
            ):
                raise _inner
        assert exc_info.value is _inner

    def test_no_exception(self):
        with ota_errors.raise_as_ota_error(
            ota_errors.PostUpdateFailed, "post-update failed", module=__name__
        ):
            pass
