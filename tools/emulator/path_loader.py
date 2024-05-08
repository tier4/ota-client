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


# NOTE: this file should only be loaded once by the program entry!


###### load path ######
def _path_load():
    import sys
    from pathlib import Path

    project_base = Path(__file__).absolute().parent.parent
    sys.path.extend([str(project_base), str(project_base / "app")])


_path_load()
######
