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


import logging

from otaclient import __version__
from otaclient.configs.cfg import ecu_info
from otaclient.log_setting import configure_logging

logger = logging.getLogger("otaclient")

configure_logging()

if __name__ == "__main__":  # pragma: no cover
    from otaclient.grpc.main import main

    logger.info("started")
    logger.info(f"otaclient version: {__version__}")
    logger.info(f"ecu_info.yaml: \n{ecu_info}")

    main()
