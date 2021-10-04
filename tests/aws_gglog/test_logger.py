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

    def test_get_instance(self):
        from aws_gglog.logger import _BaseLogger

        _BaseLogger._instance = None
        assert _BaseLogger.get_instance() == _BaseLogger.get_instance()

    def test_singleton(self):
        from aws_gglog.logger import _BaseLogger

        with pytest.raises(Exception) as e:
            _BaseLogger._instance = None
            _BaseLogger.get_instance()
            _BaseLogger()

        assert str(e.value) == "BaseLogger is singleton"

    def test_create_instance_with_cloudwatch_handler(self, mocker):
        from aws_gglog.logger import _BaseLogger
        import watchtower

        mocker.patch("boto3_session.Boto3Session.get_session", return_value="session")
        mocker.patch(
            "boto3_session.Boto3Session.parse_config",
            return_value={
                "ca_cert": "/foo/bar/ca_cert.pem",
                "private_key": "/foo/bar/private_key.pem",
                "cert": "/foo/bar/cert.pem",
                "region": "ap-northeast-1",
                "thing_name": "foo-bar",
            },
        )
        watchtower.CloudWatchLogHandler = mocker.MagicMock()

        os.environ["AWS_GREENGRASS_CONFIG"] = "/foo/bar/config.json"
        os.environ["AWS_CREDENTIAL_PROVIDER_ENDPOINT"] = "foo.bar"
        os.environ["AWS_ROLE_ALIAS"] = "foo-bar"
        os.environ["AWS_CLOUDWATCH_LOG_GROUP"] = "foo/bar"

        _BaseLogger._instance = None
        _BaseLogger()

        watchtower.CloudWatchLogHandler.assert_called_once_with(
            boto3_session="session",
            log_group="foo/bar",
            stream_name="{strftime:%Y/%m/%d}/foo-bar",
            create_log_group=True,
            create_log_stream=True,
        )

    def test_create_instance_without_cloudwatch_handler(self, mocker):
        from aws_gglog.logger import _BaseLogger
        import watchtower

        watchtower.CloudWatchLogHandler = mocker.MagicMock()

        _BaseLogger._instance = None
        _BaseLogger()

        watchtower.CloudWatchLogHandler.assert_not_called()
