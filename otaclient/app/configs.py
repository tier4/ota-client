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
"""Runtime configs and consts for otaclient."""


from __future__ import annotations
from pathlib import Path
from otaclient import __file__ as _otaclient__init__
from otaclient.configs.app_cfg import app_config
from otaclient.configs.ota_service_cfg import service_config

# NOTE: temp place holder, will be cleanup in the future
service_config = service_config
config = app_config


OTACLIENT_PACKAGE_ROOT = Path(_otaclient__init__).parent

# NOTE: VERSION file is installed under otaclient package root
EXTRA_VERSION_FILE = str(OTACLIENT_PACKAGE_ROOT / "version.txt")
