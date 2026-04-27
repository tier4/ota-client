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
"""Unit tests for the v1-vs-legacy OTA image detection heuristic.

Scope: the `check_if_ota_image_v1` decision tree given a stubbed HTTP
client. The HTTP layer is mocked, so this stays in the `unit/` tier.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from pytest_mock import MockerFixture

from ota_metadata.utils import detect_ota_image_ver
from ota_metadata.utils.detect_ota_image_ver import check_if_ota_image_v1
from otaclient_common.downloader import DownloaderPool

MODULE = detect_ota_image_ver.__name__


@pytest.fixture
def mock_downloader_pool(mocker: MockerFixture):
    pool = mocker.MagicMock(spec=DownloaderPool)
    downloader = mocker.MagicMock()
    downloader._force_http = False
    downloader._session = mocker.MagicMock()
    pool.get_instance.return_value = downloader
    return pool, downloader


@pytest.mark.parametrize(
    "status_code, expected",
    (
        pytest.param(HTTPStatus.OK, True, id="200_v1"),
        pytest.param(HTTPStatus.UNAUTHORIZED, False, id="401_legacy"),
        pytest.param(HTTPStatus.FORBIDDEN, False, id="403_legacy"),
        pytest.param(HTTPStatus.NOT_FOUND, False, id="404_legacy"),
    ),
)
def test_check_if_ota_image_v1_status(
    status_code, expected, mock_downloader_pool, mocker: MockerFixture
):
    """The hint-file response code drives the v1 decision."""
    pool, downloader = mock_downloader_pool

    resp = mocker.MagicMock()
    resp.status_code = status_code
    downloader._session.get.return_value = resp

    assert (
        check_if_ota_image_v1("https://example.com/ota", downloader_pool=pool)
        is expected
    )
    pool.release_instance.assert_called_once()


def test_check_if_ota_image_v1_force_http_url_rewrite(
    mock_downloader_pool, mocker: MockerFixture
):
    """`_force_http` rewrites the scheme on the probe URL."""
    pool, downloader = mock_downloader_pool
    downloader._force_http = True

    resp = mocker.MagicMock()
    resp.status_code = HTTPStatus.OK
    downloader._session.get.return_value = resp

    assert (
        check_if_ota_image_v1("https://example.com/ota", downloader_pool=pool) is True
    )
    called_url = downloader._session.get.call_args[0][0]
    assert called_url.startswith("http://")
    assert called_url.endswith("oci-layout")


def test_check_if_ota_image_v1_retries_then_gives_up(
    mock_downloader_pool, mocker: MockerFixture
):
    """Transient failures (non-categorised statuses + exceptions) retry RETRY_TIMES then fail."""
    pool, downloader = mock_downloader_pool

    resp_500 = mocker.MagicMock()
    resp_500.status_code = HTTPStatus.INTERNAL_SERVER_ERROR
    downloader._session.get.return_value = resp_500

    mocker.patch(f"{MODULE}.time.sleep")

    assert (
        check_if_ota_image_v1("https://example.com/ota", downloader_pool=pool) is False
    )
    assert (
        downloader._session.get.call_count
        == detect_ota_image_ver.RETRY_TIMES
    )
    pool.release_instance.assert_called_once()
