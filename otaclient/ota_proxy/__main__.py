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


import argparse
import asyncio
import logging
import uvloop

from . import run_otaproxy
from .config import config as cfg

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="ota_proxy",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="ota_proxy server with local cache feature",
    )
    parser.add_argument("--host", help="server listen ip", default="0.0.0.0")
    parser.add_argument("--port", help="server listen port", default=8080, type=int)
    parser.add_argument(
        "--upper-proxy",
        help="upper proxy that used for requesting remote",
        default="",
    )
    parser.add_argument(
        "--enable-cache",
        help="enable local ota cache",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--enable-https",
        help="enable HTTPS when retrieving data from remote",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--init-cache",
        help="cleanup remaining cache if any",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--cache-dir",
        help="where to store the cache entries",
        default=cfg.BASE_DIR,
    )
    parser.add_argument(
        "--cache-db-file",
        help="the location of cache db sqlite file",
        default=cfg.DB_FILE,
    )
    args = parser.parse_args()

    logger.info(f"launch ota_proxy at {args.host}:{args.port}")
    uvloop.install()
    asyncio.run(
        run_otaproxy(
            host=args.host,
            port=args.port,
            cache_dir=args.cache_dir,
            cache_db_f=args.cache_db_file,
            enable_cache=args.enable_cache,
            upper_proxy=args.upper_proxy,
            enable_https=args.enable_https,
            init_cache=args.init_cache,
        )
    )
