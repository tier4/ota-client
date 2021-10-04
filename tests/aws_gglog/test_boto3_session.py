import os
import pytest


class TestBoto3Session:
    def test_parse_config(self, shared_datadir):
        from aws_gglog.boto3_session import Boto3Session

        config = Boto3Session.parse_config(
            os.path.join(shared_datadir, "greengrass/config.json")
        )
        assert config.get("ca_cert") == "/greengrass/certs/root.ca.pem"
        assert config.get("private_key") == "/greengrass/certs/gg.private.key"
        assert config.get("cert") == "/greengrass/certs/gg.cert.pem"
        assert config.get("region") == "ap-northeast-1"
        assert config.get("thing_name") == "foo-bar"

    def test_parse_config_no_file(self):
        from aws_gglog.boto3_session import Boto3Session

        with pytest.raises(Exception) as e:
            Boto3Session.parse_config("no_file")

        assert str(e.value) == "[Errno 2] No such file or directory: 'no_file'"

    def test_parse_config_invalid_thing_arn(self, shared_datadir):
        from aws_gglog.boto3_session import Boto3Session

        with pytest.raises(Exception) as e:
            Boto3Session.parse_config(
                os.path.join(shared_datadir, "greengrass/config_invalid_thingArn.json")
            )

        assert str(e.value) == "invalid thing arn: thing_arn=thing/foo-bar"

    def test__refresh_credentials(self, mocker, shared_datadir):
        import requests
        from aws_gglog.boto3_session import Boto3Session

        resp_mock = mocker.MagicMock()
        resp_mock.text = '{"credentials":{"accessKeyId":"123","secretAccessKey":"abc","sessionToken":"ABC","expiration":"2021-10-01T09:18:06Z"}}'
        requests.get = mocker.MagicMock(return_value=resp_mock)
        requests.raise_for_status = mocker.MagicMock()

        session = Boto3Session(
            os.path.join(os.path.join(shared_datadir, "greengrass/config.json")),
            "https://example.com",
            "example_role_alias",
        )
        got_credential = session._refresh()
        want_credential = {
            "access_key": "123",
            "secret_key": "abc",
            "token": "ABC",
            "expiry_time": "2021-10-01T09:18:06Z",
        }

        assert got_credential == want_credential
