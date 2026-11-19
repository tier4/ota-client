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

import json
from http import HTTPStatus

import pytest
from ota_image_libs.v1.image_manifest.schema import ImageIdentifier, OTAReleaseKey
from pytest_mock import MockerFixture

from ota_metadata.utils import detect_ota_image_ver
from ota_metadata.utils.detect_ota_image_ver import (
    check_if_ota_image_v1,
    has_parameter_set_for,
)
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
    assert downloader._session.get.call_count == detect_ota_image_ver.RETRY_TIMES
    pool.release_instance.assert_called_once()


#
# ------ a parameter set as the ECU's payload ------ #
#

THIS_ECU = ImageIdentifier("autoware", OTAReleaseKey.dev)


def _index_text(artifact_type: str | None) -> str:
    _entry = {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": "sha256:" + "5" * 64,
        "size": 900,
        "annotations": {
            "vnd.tier4.pilot-auto.platform.ecu": "autoware",
            "vnd.tier4.ota.release-key": "dev",
        },
    }
    if artifact_type:
        _entry["artifactType"] = artifact_type
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [_entry],
            "annotations": {"vnd.tier4.ota.ota-image-builder.version": "t"},
        }
    )


def _index_response(mocker, status=HTTPStatus.OK, body: bytes = b""):
    resp = mocker.MagicMock()
    resp.status_code = status
    resp.__enter__.return_value = resp
    resp.raw.read.return_value = body
    return resp


@pytest.mark.parametrize(
    "artifact_type, ecu, expected",
    (
        pytest.param(
            "application/vnd.tier4.ota.parameter-set.v1", THIS_ECU, True, id="set"
        ),
        pytest.param(None, THIS_ECU, False, id="file_based"),
        pytest.param(
            "application/vnd.tier4.ota.parameter-set.v1",
            ImageIdentifier("perception", OTAReleaseKey.dev),
            False,
            id="another_ecus_set",
        ),
    ),
)
def test_has_parameter_set_for(
    artifact_type, ecu, expected, mock_downloader_pool, mocker: MockerFixture
):
    """The payload kind of the ECU's entry picks the updater."""
    pool, downloader = mock_downloader_pool
    downloader._session.get.return_value = _index_response(
        mocker, body=_index_text(artifact_type).encode()
    )

    assert (
        has_parameter_set_for("https://example.com/ota", ecu, downloader_pool=pool)
        is expected
    )
    assert (
        downloader._session.get.call_args.args[0]
        == "https://example.com/ota/index.json"
    )
    pool.release_instance.assert_called_once()


@pytest.mark.parametrize(
    "response",
    (
        pytest.param({"status": HTTPStatus.NOT_FOUND}, id="404"),
        pytest.param({"status": HTTPStatus.FORBIDDEN}, id="403"),
        pytest.param({"body": b"{not an index"}, id="not_an_index"),
    ),
)
def test_has_parameter_set_for_gives_up_at_once_on_what_will_not_pass(
    response, mock_downloader_pool, mocker: MockerFixture
):
    """The updater the image would have gone to anyway reports what is wrong."""
    pool, downloader = mock_downloader_pool
    _sleep = mocker.patch(f"{MODULE}.time.sleep")
    downloader._session.get.return_value = _index_response(mocker, **response)

    assert (
        has_parameter_set_for("https://example.com/ota", THIS_ECU, downloader_pool=pool)
        is False
    )
    assert downloader._session.get.call_count == 1
    _sleep.assert_not_called()


def test_has_parameter_set_for_retries_what_may_pass(
    mock_downloader_pool, mocker: MockerFixture
):
    pool, downloader = mock_downloader_pool
    mocker.patch(f"{MODULE}.time.sleep")
    downloader._session.get.side_effect = [
        _index_response(mocker, status=HTTPStatus.SERVICE_UNAVAILABLE),
        ConnectionError("the link dropped"),
        _index_response(
            mocker,
            body=_index_text("application/vnd.tier4.ota.parameter-set.v1").encode(),
        ),
    ]
    assert (
        has_parameter_set_for("https://example.com/ota", THIS_ECU, downloader_pool=pool)
        is True
    )


def test_has_parameter_set_for_does_not_read_more_than_an_index(
    mock_downloader_pool, mocker: MockerFixture
):
    """A body past the bound is not taken, even a valid index of a set."""
    pool, downloader = mock_downloader_pool
    _body = _index_text("application/vnd.tier4.ota.parameter-set.v1").encode()
    mocker.patch(f"{MODULE}.INDEX_MAX_SIZE", len(_body) - 1)
    _resp = _index_response(mocker, body=_body)
    downloader._session.get.return_value = _resp
    assert (
        has_parameter_set_for("https://example.com/ota", THIS_ECU, downloader_pool=pool)
        is False
    )
    _resp.raw.read.assert_called_once_with(len(_body), decode_content=True)


def test_has_parameter_set_for_cannot_tell_once_the_retries_are_spent(
    mock_downloader_pool, mocker: MockerFixture
):
    """The caller reports a failed download instead of guessing the kind."""
    pool, downloader = mock_downloader_pool
    mocker.patch(f"{MODULE}.time.sleep")
    downloader._session.get.return_value = _index_response(
        mocker, status=HTTPStatus.SERVICE_UNAVAILABLE
    )
    assert (
        has_parameter_set_for("https://example.com/ota", THIS_ECU, downloader_pool=pool)
        is None
    )
    assert downloader._session.get.call_count == detect_ota_image_ver.RETRY_TIMES
    pool.release_instance.assert_called_once()
