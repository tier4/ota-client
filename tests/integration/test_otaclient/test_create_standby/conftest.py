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
"""Local re-exports for create_standby integration tests.

The shared file_table fixtures (`fst_db_helper`, `ab_slots_for_inplace`,
`resource_dir`) and the `SlotAB` type live in the top-level
`tests/conftest.py` (the /ota-image fixture tree they build from is baked into
the test container by docker/test_base/Dockerfile). `SlotAB` is re-exported
here so test modules import it via `from .conftest`.
"""

from __future__ import annotations

from tests.conftest import SlotAB  # noqa: F401
