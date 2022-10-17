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


import os
import pytest


class TestGreengarssConfig:
    def test_parse_config(self, shared_datadir):
        from otaclient.aws_iot_log_server.greengrass_config import GreengrassConfig

        config = GreengrassConfig.parse_config(
            os.path.join(shared_datadir, "greengrass/config.json"),
            None,
        )
        assert config.get("ca_cert") == "/greengrass/certs/root.ca.pem"
        assert config.get("private_key") == "/greengrass/certs/gg.private.key"
        assert config.get("cert") == "/greengrass/certs/gg.cert.pem"
        assert config.get("region") == "ap-northeast-1"
        assert config.get("thing_name") == "foo-bar"

    def test_parse_config_no_file(self):
        from otaclient.aws_iot_log_server.greengrass_config import GreengrassConfig

        with pytest.raises(Exception) as e:
            GreengrassConfig.parse_config("no_file", None)

        assert str(e.value) == "[Errno 2] No such file or directory: 'no_file'"

    def test_parse_config_invalid_thing_arn(self, shared_datadir):
        from otaclient.aws_iot_log_server.greengrass_config import GreengrassConfig

        with pytest.raises(Exception) as e:
            GreengrassConfig.parse_config(
                os.path.join(shared_datadir, "greengrass/config_invalid_thingArn.json"),
                None,
            )

        assert str(e.value) == "invalid thing arn: thing_arn=thing/foo-bar"

    def test_parse_config_v2(self, shared_datadir):
        from otaclient.aws_iot_log_server.greengrass_config import GreengrassConfig

        config = GreengrassConfig.parse_config(
            os.path.join(shared_datadir, "greengrass/config.json"),
            os.path.join(shared_datadir, "greengrass/config.yaml"),
        )
        assert config.get("ca_cert") == "/greengrass/certs/root.ca.pem"
        assert config.get("private_key") == "/greengrass/certs/gg.private.key"
        assert config.get("cert") == "/greengrass/certs/gg.cert.pem"
        assert config.get("region") == "ap-northeast-1"
        assert config.get("thing_name") == "foo-bar-v2"

    def test_parse_config_illegal_v2(self, shared_datadir):
        from otaclient.aws_iot_log_server.greengrass_config import GreengrassConfig

        config = GreengrassConfig.parse_config(
            os.path.join(shared_datadir, "greengrass/config.json"),
            os.path.join(shared_datadir, "greengrass/config.json"),  # illegal v2 format
        )
        assert config.get("ca_cert") == "/greengrass/certs/root.ca.pem"
        assert config.get("private_key") == "/greengrass/certs/gg.private.key"
        assert config.get("cert") == "/greengrass/certs/gg.cert.pem"
        assert config.get("region") == "ap-northeast-1"
        assert config.get("thing_name") == "foo-bar"
