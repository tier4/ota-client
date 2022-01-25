import os
import pytest


class TestGreengarssConfig:
    def test_parse_config(self, shared_datadir):
        from aws_gglog.greengrass_config import GreengrassConfig

        config = GreengrassConfig.parse_config(
            os.path.join(shared_datadir, "greengrass/config.json")
        )
        assert config.get("ca_cert") == "/greengrass/certs/root.ca.pem"
        assert config.get("private_key") == "/greengrass/certs/gg.private.key"
        assert config.get("cert") == "/greengrass/certs/gg.cert.pem"
        assert config.get("region") == "ap-northeast-1"
        assert config.get("thing_name") == "foo-bar"

    def test_parse_config_no_file(self):
        from aws_gglog.greengrass_config import GreengrassConfig

        with pytest.raises(Exception) as e:
            GreengrassConfig.parse_config("no_file")

        assert str(e.value) == "[Errno 2] No such file or directory: 'no_file'"

    def test_parse_config_invalid_thing_arn(self, shared_datadir):
        from aws_gglog.greengrass_config import GreengrassConfig

        with pytest.raises(Exception) as e:
            GreengrassConfig.parse_config(
                os.path.join(shared_datadir, "greengrass/config_invalid_thingArn.json")
            )

        assert str(e.value) == "invalid thing arn: thing_arn=thing/foo-bar"
