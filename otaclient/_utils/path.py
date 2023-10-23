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


import os.path
from otaclient._utils.typing import StrOrPath


def replace_root(path: StrOrPath, old_root: StrOrPath, new_root: StrOrPath) -> str:
    """Replace a <path> relative to <old_root> to <new_root>.

    For example, if path="/abc", old_root="/", new_root="/new_root",
    then we will have "/new_root/abc".
    """
    # normalize all the input args
    path = os.path.normpath(path)
    old_root = os.path.normpath(old_root)
    new_root = os.path.normpath(new_root)

    if not (old_root.startswith("/") and new_root.startswith("/")):
        raise ValueError(f"{old_root=} and/or {new_root=} is not valid root")
    if os.path.commonpath([path, old_root]) != old_root:
        raise ValueError(f"{old_root=} is not the root of {path=}")
    return os.path.join(new_root, os.path.relpath(path, old_root))
