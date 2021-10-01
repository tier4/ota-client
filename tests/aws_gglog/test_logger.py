import os
import pytest


class Test_BaseLogger:
    def test__gen_log_stream_name(self, shared_datadir):
        from aws_gglog.logger import _BaseLogger

        name = _BaseLogger._get_stream_name(
            os.path.join(os.path.join(shared_datadir, "greengrass/config.json"))
        )
        assert name == "{strftime:%Y/%m/%d}/foo-bar"

    def test__get_config(self):
        from aws_gglog.logger import _BaseLogger

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

    def test_get_instance(self, mocker):
        from aws_gglog.logger import _BaseLogger

        _BaseLogger._set_cloudwatch_log_handler = mocker.MagicMock()
        assert _BaseLogger.get_instance() == _BaseLogger.get_instance()

    def test_singleton(self, mocker):
        from aws_gglog.logger import _BaseLogger

        _BaseLogger._set_cloudwatch_log_handler = mocker.MagicMock()

        with pytest.raises(Exception) as e:
            _BaseLogger.get_instance()
            _BaseLogger()

        assert str(e.value) == "BaseLogger is singleton"
