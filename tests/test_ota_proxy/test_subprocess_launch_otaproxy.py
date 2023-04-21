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


from pathlib import Path
from otaclient.ota_proxy import subprocess_start_otaproxy


def test_subprocess_start_otaproxy(self, tmp_path: Path):
    (ota_cache_dir := tmp_path / "ota-cache").mkdir(exist_ok=True)
    ota_cache_db = ota_cache_dir / "cache_db"
    otaproxy_subprocess_initialized = tmp_path / "otaproxy_started"

    def _subprocess_init():
        otaproxy_subprocess_initialized.touch()

    otaproxy_subprocess = subprocess_start_otaproxy(
        host="127.0.0.1",
        port=8082,
        init_cache=True,
        cache_dir=str(ota_cache_dir),
        cache_db_f=str(ota_cache_db),
        upper_proxy="",
        enable_cache=True,
        enable_https=False,
        subprocess_init=_subprocess_init,
    )
    assert otaproxy_subprocess.is_alive()
    assert otaproxy_subprocess_initialized.is_file()

    otaproxy_subprocess.terminate()
