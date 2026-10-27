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


from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class DownloadInfo:
    url: str
    dst: Path
    original_size: int = 0
    """NOTE: we are using transparent decompression, so we always use the original_size."""
    digest_alg: Optional[str] = None
    digest: Optional[str] = None
    compression_alg: Optional[str] = None
    resume: bool = False
    """Continue what an interrupted download left at <dst> with a ranged request,
    instead of fetching the file again from the start. Needs <digest> and
    <original_size>, and not for a file decompressed as it arrives (there is no byte
    offset in a decompressed stream to continue from)."""
