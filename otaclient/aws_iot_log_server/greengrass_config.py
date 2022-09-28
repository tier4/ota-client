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
import json
import re

from .configs import LOG_FORMAT

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
_sh = logging.StreamHandler()
fmt = logging.Formatter(fmt=LOG_FORMAT)
_sh.setFormatter(fmt)
logger.addHandler(_sh)


class GreengrassConfig:
    @staticmethod
    def parse_config(config) -> dict:
        try:
            with open(config) as f:
                cfg = json.load(f)
        except FileNotFoundError:
            logger.exception(f"config file is not found: file={config}")
            raise
        except json.JSONDecodeError as e:
            logger.exception(f"invalid json format: {e}")
            raise

        ca_path = cfg.get("crypto", {}).get("caPath")
        private_key_path = (
            cfg.get("crypto", {})
            .get("principals", {})
            .get("IoTCertificate", {})
            .get("privateKeyPath")
        )
        certificate_path = (
            cfg.get("crypto", {})
            .get("principals", {})
            .get("IoTCertificate", {})
            .get("certificatePath")
        )
        thing_arn = cfg.get("coreThing", {}).get("thingArn")

        strs = thing_arn.split(":", 6)
        if len(strs) != 6:
            logger.error(f"invalid thing arn: thing_arn={thing_arn}")
            raise Exception(f"invalid thing arn: thing_arn={thing_arn}")

        region = strs[3]
        thing_name = strs[5]

        def remove_prefix(s, prefix):
            return re.sub(f"^{prefix}", "", s)

        return {
            "ca_cert": remove_prefix(ca_path, "file://"),
            "private_key": remove_prefix(private_key_path, "file://"),
            "cert": remove_prefix(certificate_path, "file://"),
            "region": region,
            "thing_name": remove_prefix(thing_name, "thing/"),
        }
