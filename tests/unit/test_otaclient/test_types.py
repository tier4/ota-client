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

from otaclient._types import ClientUpdateRequestV2, UpdateRequestV2

DUMMY_COOKIES = '{"CloudFront-Signature": "dummy-secret-signature"}'


@pytest.mark.parametrize("request_cls", [UpdateRequestV2, ClientUpdateRequestV2])
def test_update_request_cookies_not_in_repr(request_cls):
    request = request_cls(
        request_id="dummy-request-id",
        session_id="dummy-session-id",
        version="789.x",
        url_base="https://example.com/ota",
        cookies_json=DUMMY_COOKIES,
    )

    for _output in (str(request), repr(request), f"{request=}"):
        assert "dummy-secret-signature" not in _output
        assert "cookies_json" not in _output
        assert "https://example.com/ota" in _output
    assert request.cookies_json == DUMMY_COOKIES
