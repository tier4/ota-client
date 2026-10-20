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

import logging

import pytest

from otaclient import errors as ota_errors
from otaclient._utils import get_traceback
from otaclient.ota_core._common import prepare_cookies


def test_prepare_cookies():
    assert prepare_cookies('{"CloudFront-Signature": "abc"}') == {
        "CloudFront-Signature": "abc"
    }


@pytest.mark.parametrize(
    "cookies_json",
    [
        # not a valid json
        '{"CloudFront-Signature": "dummy-secret-signature"',
        # not a json object
        '["dummy-secret-signature"]',
    ],
)
def test_prepare_cookies_invalid_not_leaking(cookies_json, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ota_errors.InvalidUpdateRequest) as exc_info:
        prepare_cookies(cookies_json)

    assert "dummy-secret-signature" not in caplog.text
    assert "dummy-secret-signature" not in get_traceback(exc_info.value)
