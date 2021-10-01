import os


class Test_BaseLogger:
    def test__gen_log_stream_name(self, shared_datadir):
        from logger import _BaseLogger

        name = _BaseLogger._get_stream_name(
            os.path.join(os.path.join(shared_datadir, "greengrass/config.json"))
        )
        assert name == "{strftime:%Y/%m/%d}/foo-bar"

    def test__get_config(self):
        from logger import _BaseLogger

        keys = (
            "AWS_GREENGRASS_CONFIG",
            "AWS_CREDENTIAL_PROVIDER_ENDPOINT",
            "AWS_ROLE_ALIAS",
            "AWS_CLOUDWATCH_LOG_GROUP",
        )
        for key in keys:
            os.environ[key] = f"{key.lower()}"

        config = _BaseLogger._get_config()
        assert config == {key: key.lower() for key in keys}
