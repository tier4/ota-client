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
"""Entrypoint of otaclient."""


from __future__ import annotations

import logging
import os
import sys
import time

logger = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 2  # seconds


def main() -> None:  # pragma: no cover
    from otaclient._logging import configure_logging

    # configure logging before any code being executed
    configure_logging()

    logger.info("started")
    logger.info(f"env.preparing_downloaded_dynamic_ota_client: {os.getenv('hoge')}")

    if os.getenv("hoge") == "yes":
        logger.info("in dynamic client preparation process ...")
        while True:
            time.sleep(HEALTH_CHECK_INTERVAL)

    while True:
        time.sleep(HEALTH_CHECK_INTERVAL)
        # launch the dynamic client preparation process
        try:
            logger.info("preparing dynamic client package ...")
            #            close_all_logging_handlers()

            preparing_env = os.environ.copy()
            preparing_env["hoge"] = "yes"
            # Execute with the modified environment
            os.execve(
                path=sys.executable,
                argv=[sys.executable, "-m", "otaclient"],
                env=preparing_env,
            )
        except Exception as e:
            logger.exception(f"Failed during dynamic client preparation: {e}")


if __name__ == "__main__":
    main()
