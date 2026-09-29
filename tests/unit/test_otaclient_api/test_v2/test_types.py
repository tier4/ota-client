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

from __future__ import annotations

import pytest

from otaclient_api.v2 import _types as api_types


class TestStatusResponseEcuV2Properties:
    """Test the is_* status properties on StatusResponseEcuV2."""

    @pytest.mark.parametrize(
        "ota_status, expected",
        [
            (api_types.StatusOta.UPDATING, True),
            (api_types.StatusOta.SUCCESS, False),
            (api_types.StatusOta.ABORTING, False),
        ],
    )
    def test_is_in_update(self, ota_status, expected):
        ecu = api_types.StatusResponseEcuV2(ecu_id="ecu_1", ota_status=ota_status)
        assert ecu.is_in_update is expected

    @pytest.mark.parametrize(
        "ota_status, expected",
        [
            (api_types.StatusOta.CLIENT_UPDATING, True),
            (api_types.StatusOta.UPDATING, False),
        ],
    )
    def test_is_in_client_update(self, ota_status, expected):
        ecu = api_types.StatusResponseEcuV2(ecu_id="ecu_1", ota_status=ota_status)
        assert ecu.is_in_client_update is expected

    @pytest.mark.parametrize(
        "ota_status, expected",
        [
            (api_types.StatusOta.ABORTING, True),
            (api_types.StatusOta.UPDATING, False),
            (api_types.StatusOta.ABORTED, False),
            (api_types.StatusOta.SUCCESS, False),
        ],
    )
    def test_is_aborting(self, ota_status, expected):
        ecu = api_types.StatusResponseEcuV2(ecu_id="ecu_1", ota_status=ota_status)
        assert ecu.is_aborting is expected

    @pytest.mark.parametrize(
        "ota_status, expected",
        [
            (api_types.StatusOta.ABORTED, True),
            (api_types.StatusOta.ABORTING, False),
            (api_types.StatusOta.UPDATING, False),
            (api_types.StatusOta.SUCCESS, False),
        ],
    )
    def test_is_aborted(self, ota_status, expected):
        ecu = api_types.StatusResponseEcuV2(ecu_id="ecu_1", ota_status=ota_status)
        assert ecu.is_aborted is expected

    @pytest.mark.parametrize(
        "ota_status, expected",
        [
            (api_types.StatusOta.FAILURE, True),
            (api_types.StatusOta.SUCCESS, False),
        ],
    )
    def test_is_failed(self, ota_status, expected):
        ecu = api_types.StatusResponseEcuV2(ecu_id="ecu_1", ota_status=ota_status)
        assert ecu.is_failed is expected

    @pytest.mark.parametrize(
        "ota_status, expected",
        [
            (api_types.StatusOta.SUCCESS, True),
            (api_types.StatusOta.FAILURE, False),
        ],
    )
    def test_is_success(self, ota_status, expected):
        ecu = api_types.StatusResponseEcuV2(ecu_id="ecu_1", ota_status=ota_status)
        assert ecu.is_success is expected


DUMMY_COOKIES = '{"CloudFront-Signature": "dummy-secret-signature"}'


@pytest.mark.parametrize(
    "request_cls, request_ecu_cls",
    [
        (api_types.UpdateRequest, api_types.UpdateRequestEcu),
        (api_types.ClientUpdateRequest, api_types.ClientUpdateRequestEcu),
    ],
)
def test_update_request_cookies_redacted(request_cls, request_ecu_cls):
    request = request_cls(
        ecu=[
            request_ecu_cls(
                ecu_id="autoware",
                version="789.x",
                url="https://example.com/ota",
                cookies=DUMMY_COOKIES,
            )
        ],
        request_id="dummy-request-id",
    )

    for _output in (str(request), repr(request), f"{request}"):
        assert "dummy-secret-signature" not in _output
        assert "<redacted>" in _output
        # other fields are still shown
        assert "autoware" in _output
        assert "https://example.com/ota" in _output

    # the actual value is still available
    assert request.ecu[0].cookies == DUMMY_COOKIES
    assert request.export_pb().ecu[0].cookies == DUMMY_COOKIES
