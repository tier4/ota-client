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


import time
from functools import partial
from pathlib import Path
from typing import Any, Dict
from otaclient.ota_proxy import subprocess_otaproxy_launcher, OTAProxyContextProto


class _DummyOTAProxyContext(OTAProxyContextProto):
    def __init__(self, sentinel) -> None:
        self.sentinel = sentinel

    @property
    def extra_kwargs(self) -> Dict[str, Any]:
        return {}

    def __enter__(self):
        _subprocess_init(self.sentinel)
        return self

    def __exit__(self, __exc_type, __exc_value, __traceback):
        return


def _subprocess_init(_sentinel_file):
    Path(_sentinel_file).touch()


def test_subprocess_start_otaproxy(tmp_path: Path):
    # --- setup --- #
    (ota_cache_dir := tmp_path / "ota-cache").mkdir(exist_ok=True)
    ota_cache_db = ota_cache_dir / "cache_db"
    subprocess_init_sentinel = tmp_path / "otaproxy_started"

    # --- execution --- #
    _subprocess_entry = subprocess_otaproxy_launcher(
        subprocess_ctx=_DummyOTAProxyContext(sentinel=str(subprocess_init_sentinel))
    )
    otaproxy_subprocess = _subprocess_entry(
        host="127.0.0.1",
        port=8082,
        init_cache=True,
        cache_dir=str(ota_cache_dir),
        cache_db_f=str(ota_cache_db),
        upper_proxy="",
        enable_cache=True,
        enable_https=False,
    )
    time.sleep(3)  # wait for subprocess to finish up initializing

    # --- assertion --- #
    try:
        assert otaproxy_subprocess.is_alive()
        assert subprocess_init_sentinel.is_file()
    finally:
        otaproxy_subprocess.terminate()
