# Copyright 2023 TIER IV, INC. All rights reserved.
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


import yaml
import zstandard
from pathlib import Path
from typing import Dict, Callable
from ..configs import config as cfg
from .. import log_setting

logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class Firmware:
    """
    config_file example:
    - `file` is expected to be located under the `config_file` directory.
    - only zstd is support for now as compression.
    - file is expected be extracted and copied to the appropriate partition.
    - partitions[0] is for slot_a and partitions[1] is for slot_b
    ---
    version: 1
    firmwares:
      - file: rce.zst
        compression: zstd
        partitions:
          - /dev/mmcblk0p21
          - /dev/mmcblk0p22
      - file: xusb.zst
        compression: zstd
        partitions:
          - /dev/mmcblk0p19
          - /dev/mmcblk0p20
      ...
    """

    def __init__(self, config_file: Path):
        self._config_file: Path = config_file

    def update(self, slot_a: bool):
        if not self._config_file.is_file():
            # just emit warning for backward compatibility.
            logger.warning(f"{self._config_file} doesn't exist")
            return
        config = yaml.safe_load(self._config_file.read_text())
        logger.info(f"{config=}")
        for fw in config["firmwares"]:
            if fw["compression"] != "zstd":
                raise ValueError(f"illegal compression: {fw['compression']=}")
            if len(fw["partitions"]) != 2:
                raise ValueError(f"illegal partitions length: {len(fw['partitions'])=}")
            self._extract_and_copy(fw, slot_a)

    def _extract_and_copy(self, firmware: Dict, slot_a: bool):
        parent = self._config_file.parent
        dctx = zstandard.ZstdDecompressor()
        ifile = parent / firmware["file"]
        ofile = firmware["partitions"][0 if slot_a else 1]
        self._copy(ifile, ofile, dctx.copy_stream)

    def _copy(self, ifile: Path, ofile: Path, copy_func: Callable):
        with open(ifile, "rb") as ifh, open(ofile, "wb") as ofh:
            logger.info(f"{ifile=}, {ofile=}")
            copy_func(ifh, ofh)
