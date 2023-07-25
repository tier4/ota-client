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


from typing import Any, Dict
import pytest
from otaclient.ota_proxy import OTAFileCacheControl


@pytest.mark.parametrize(
    "raw_str, expected",
    (
        ("no_cache", OTAFileCacheControl(no_cache=True)),
        ("retry_caching", OTAFileCacheControl(retry_caching=True)),
        (
            "no_cache, file_sha256=sha256value, file_compression_alg=zst",
            OTAFileCacheControl(
                no_cache=True,
                file_sha256="sha256value",
                file_compression_alg="zst",
            ),
        ),
    ),
)
def test__parse_header(raw_str, expected):
    assert OTAFileCacheControl.parse_header(raw_str) == expected


@pytest.mark.parametrize(
    "kwargs, expected",
    (
        ({"no_cache": True, "retry_caching": False}, "no_cache"),
        (
            {"no_cache": True, "file_sha256": "sha256_value"},
            "no_cache,file_sha256=sha256_value",
        ),
        (
            {
                "retry_caching": True,
                "file_sha256": "sha256_value",
                "file_compression_alg": "zst",
            },
            "retry_caching,file_sha256=sha256_value,file_compression_alg=zst",
        ),
    ),
)
def test__export_kwargs_as_header(kwargs: Dict[str, Any], expected: str):
    assert OTAFileCacheControl.export_kwargs_as_header(**kwargs) == expected


@pytest.mark.parametrize(
    "_input, kwargs, expected",
    (
        (
            "file_sha256=sha256_value,file_compression_alg=zst",
            {"no_cache": True, "retry_caching": True},
            "file_sha256=sha256_value,file_compression_alg=zst,no_cache,retry_caching",
        ),
        (
            "file_sha256=sha256_value,file_compression_alg=zst",
            {"file_sha256": "new_sha256_value", "retry_caching": True},
            "file_sha256=new_sha256_value,file_compression_alg=zst,retry_caching",
        ),
        (
            "retry_caching,file_sha256=sha256_value,file_compression_alg=zst",
            {"retry_caching": False},
            "file_sha256=sha256_value,file_compression_alg=zst",
        ),
    ),
)
def test__update_header_str(_input: str, kwargs: Dict[str, Any], expected: str):
    assert OTAFileCacheControl.update_header_str(_input, **kwargs) == expected


@pytest.mark.parametrize(
    "origin, kwargs, updated",
    (
        (
            OTAFileCacheControl(retry_caching=True),
            {"no_cache": True},
            OTAFileCacheControl(retry_caching=True, no_cache=True),
        ),
        (
            OTAFileCacheControl(file_sha256="sha256"),
            {"retry_caching": True},
            OTAFileCacheControl(retry_caching=True, file_sha256="sha256"),
        ),
        (
            OTAFileCacheControl(retry_caching=True, file_sha256="sha256"),
            {"retry_caching": False},
            OTAFileCacheControl(file_sha256="sha256"),
        ),
    ),
)
def test__update_from_directives(
    origin: OTAFileCacheControl, kwargs: Dict[str, Any], updated: OTAFileCacheControl
):
    assert origin.update_from_directives(**kwargs) == updated
