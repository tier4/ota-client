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
"""Shared mocks for ota_core integration tests.

`_updater.ensure_umount` shells out to the real OS umount syscall; the updater
calls it on the session workdir during `execute()` cleanup, so integration
tests that drive the updater state machine must mock it. The CA cert dir is
repointed at `/certs` (baked into the test container image) so any code path
that consults `cfg.CERT_DPATH` works without relying on the production install
path. `fstrim_at_subprocess` is silenced so nothing tries to fstrim during
tests.

The fixture bodies are shared with the unit/e2e ota_core subtrees via
`tests._fixtures_ota_core`; they are imported (not redefined) here so the
autouse scope stays confined to this subtree.
"""

from __future__ import annotations

from tests._fixtures_ota_core import (  # noqa: F401
    mock_certs_dir,
    mock_ensure_umount,
    mock_fstrim,
)
