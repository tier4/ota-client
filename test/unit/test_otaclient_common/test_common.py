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
"""Unit tests for otaclient_common pure path helpers."""

from __future__ import annotations

import pytest

from otaclient_common import replace_root


@pytest.mark.parametrize(
    "path, old_root, new_root, expected",
    (
        pytest.param(
            "/a/canonical/fpath",
            "/",
            "/mnt/standby_mp",
            "/mnt/standby_mp/a/canonical/fpath",
            id="file-path-under-root",
        ),
        # NOTE: replace_root delegates to os.path.relpath/join, which
        #   normalises away trailing slashes — the result is unslashed.
        pytest.param(
            "/a/canonical/dpath/",
            "/",
            "/mnt/standby_mp/",
            "/mnt/standby_mp/a/canonical/dpath",
            id="dir-path-trailing-slash-normalised",
        ),
        pytest.param(
            "/a/b/c",
            "/a",
            "/mnt/new",
            "/mnt/new/b/c",
            id="non-root-old-root",
        ),
    ),
)
def test_replace_root(path, old_root, new_root, expected):
    assert replace_root(path, old_root, new_root) == expected


@pytest.mark.parametrize(
    "path, old_root, new_root",
    (
        pytest.param("/abc", "relative_root", "/new", id="old-root-not-absolute"),
        pytest.param("/abc", "/", "relative_new", id="new-root-not-absolute"),
        pytest.param(
            "/some/where", "/different/root", "/new", id="old-root-not-prefix"
        ),
    ),
)
def test_replace_root_invalid(path, old_root, new_root):
    with pytest.raises(ValueError):
        replace_root(path, old_root, new_root)
