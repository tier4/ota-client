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
"""Tests for mapping the underlying exceptions to OTA error codes."""

from __future__ import annotations

import errno
from concurrent.futures import Future

import pytest
import pytest_mock
import requests.exceptions as requests_exc
from requests import Response

from ota_metadata.errors import ImageMetadataInvalid
from otaclient import errors as ota_errors
from otaclient.ota_core import _common
from otaclient.ota_core._common import (
    download_exception_handler,
    handle_upper_proxy,
    is_caused_by_enospc,
    prepare_cookies,
)
from otaclient.ota_core._update_libs import (
    metadata_download_err_handler,
    raise_on_download_resources_failed,
)
from otaclient_common.downloader import DownloadInactiveTimeout
from otaclient_common.retry_task_map import TasksEnsureFailed


def _failed_future(exc: BaseException) -> Future:
    _fut = Future()
    _fut.set_exception(exc)
    return _fut


def _http_error(status_code: int) -> requests_exc.HTTPError:
    _resp = Response()
    _resp.status_code = status_code
    return requests_exc.HTTPError(f"HTTP {status_code}", response=_resp)


class TestDownloadExceptionHandler:
    @pytest.mark.parametrize(
        "exc, expected_error, expected_errcode",
        [
            (_http_error(401), ota_errors.UpdateRequestCookieInvalid, "E401"),
            (_http_error(403), ota_errors.UpdateRequestCookieInvalid, "E401"),
            (_http_error(404), ota_errors.OTAImageNotFound, "E404"),
            (
                OSError(errno.ENOSPC, "No space left on device"),
                ota_errors.StandbySlotInsufficientSpace,
                "E507",
            ),
        ],
    )
    def test_critical_errors(
        self,
        exc: BaseException,
        expected_error: type[ota_errors.OTAError],
        expected_errcode: str,
    ):
        with pytest.raises(expected_error) as exc_info:
            download_exception_handler(_failed_future(exc))
        assert exc_info.value.failure_errcode_str == expected_errcode

    @pytest.mark.parametrize(
        "exc",
        [_http_error(500), requests_exc.ConnectionError("conn reset")],
    )
    def test_retryable_errors(self, exc: BaseException):
        assert download_exception_handler(_failed_future(exc)) is False

    def test_succeeded(self):
        _fut = Future()
        _fut.set_result(None)
        assert download_exception_handler(_fut) is True


class TestIsCausedByENOSPC:
    def test_direct(self):
        assert is_caused_by_enospc(OSError(errno.ENOSPC, "No space left on device"))

    def test_chained(self):
        try:
            try:
                raise OSError(errno.ENOSPC, "No space left on device")
            except OSError as e:
                raise ValueError("failed to process") from e
        except ValueError as e:
            assert is_caused_by_enospc(e)

    def test_not_enospc(self):
        try:
            try:
                raise OSError(errno.EIO, "I/O error")
            except OSError as e:
                raise ValueError("failed to process") from e
        except ValueError as e:
            assert not is_caused_by_enospc(e)


class TestRaiseOnDownloadResourcesFailed:
    @pytest.mark.parametrize(
        "exc",
        [
            ota_errors.UpdateRequestCookieInvalid("403", module=__name__),
            ota_errors.OTAImageNotFound("404", module=__name__),
            ota_errors.StandbySlotInsufficientSpace("ENOSPC", module=__name__),
            ota_errors.OTAAbortSignal("abort", module=__name__),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_ota_error_passthrough(self, exc: ota_errors.OTAError):
        with pytest.raises(type(exc)) as exc_info:
            raise_on_download_resources_failed(exc)
        assert exc_info.value is exc

    def test_download_stalled(self):
        with pytest.raises(ota_errors.DownloadStalled) as exc_info:
            raise_on_download_resources_failed(
                DownloadInactiveTimeout("downloader stuck for 300 seconds, abort")
            )
        assert exc_info.value.failure_errcode_str == "E504"

    def test_other_failure(self):
        with pytest.raises(ota_errors.OTAResourceDownloadFailed) as exc_info:
            raise_on_download_resources_failed(
                TasksEnsureFailed("execution interrupted due to thread pool shutdown")
            )
        assert exc_info.value.failure_errcode_str == "E521"


class TestMetadataDownloadErrHandler:
    def test_download_stalled(self):
        with pytest.raises(ota_errors.DownloadStalled):
            with metadata_download_err_handler():
                raise DownloadInactiveTimeout("downloader stuck for 300 seconds, abort")

    def test_image_metadata_invalid(self):
        with pytest.raises(ota_errors.OTAImageInvalid):
            with metadata_download_err_handler():
                raise ImageMetadataInvalid("broken")

    def test_other_failure(self):
        with pytest.raises(ota_errors.OTAMetaDownloadFailed):
            with metadata_download_err_handler():
                raise ValueError("unknown")


class TestHandleUpperProxy:
    def test_upper_otaproxy_unreachable(self, mocker: pytest_mock.MockerFixture):
        mocker.patch(
            f"{_common.__name__}.ensure_otaproxy_start",
            side_effect=ConnectionError("timeout"),
        )
        with pytest.raises(ota_errors.UpperOTAProxyUnreachable) as exc_info:
            handle_upper_proxy("http://10.0.0.1:8082")

        _err = exc_info.value
        assert _err.failure_errcode_str == "E523"
        assert "http://10.0.0.1:8082" in _err.failure_detail


class TestPrepareCookies:
    @pytest.mark.parametrize(
        "cookies_json",
        ['["secret-cookie-value"]', "secret-cookie-value"],
    )
    def test_invalid_cookies_not_exposed(self, cookies_json: str):
        with pytest.raises(ota_errors.InvalidUpdateRequest) as exc_info:
            prepare_cookies(cookies_json)
        assert "secret-cookie-value" not in exc_info.value.get_failure_reason()
