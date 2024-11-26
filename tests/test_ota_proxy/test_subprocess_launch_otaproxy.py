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


from __future__ import annotations

import asyncio
import multiprocessing as mp
import time
from pathlib import Path

from ota_proxy import run_otaproxy


def otaproxy_process(cache_dir: str):
    ota_cache_dir = Path(cache_dir)
    ota_cache_db = ota_cache_dir / "cache_db"

    asyncio.run(
        run_otaproxy(
            host="127.0.0.1",
            port=8082,
            init_cache=True,
            cache_dir=str(ota_cache_dir),
            cache_db_f=str(ota_cache_db),
            upper_proxy="",
            enable_cache=True,
            enable_https=False,
        ),
    )


def test_subprocess_start_otaproxy(tmp_path: Path):
    # --- setup --- #
    (ota_cache_dir := tmp_path / "ota-cache").mkdir(exist_ok=True)
    ota_cache_db = ota_cache_dir / "cache_db"

    # --- execution --- #
    _spawn_ctx = mp.get_context("spawn")

    otaproxy_subprocess = _spawn_ctx.Process(
        target=otaproxy_process, args=(ota_cache_dir,)
    )
    otaproxy_subprocess.start()
    time.sleep(3)  # wait for subprocess to finish up initializing

    # --- assertion --- #
    try:
        assert otaproxy_subprocess.is_alive()
        assert ota_cache_db.is_file()
    finally:
        otaproxy_subprocess.terminate()
        otaproxy_subprocess.join()
