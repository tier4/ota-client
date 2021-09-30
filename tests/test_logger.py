import pytest
import os


class TestGBaseLogger:

    def test__gen_log_stream_name(self):
        from logger import BaseLogger

        name = BaseLogger._get_stream_name("./testdata/greengrass/config.json")
        assert name == "{strftime:%Y/%m/%d}/foo-bar"

    def test__get_config(self):
        from logger import BaseLogger
        keys = ("AWS_GREENGRASS_CONFIG",
                "AWS_CREDENTIAL_PROVIDER_ENDPOINT",
                "AWS_ROLE_ALIAS",
                "AWS_CLOUDWATCH_LOG_GROUP",
                )
        for key in keys:
            os.environ[key] = f"{key.lower()}"

        config = BaseLogger._get_config()
        assert config == {key: key.lower() for key in keys}
