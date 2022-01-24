import os


class TestBoto3Session:
    def test__refresh_credentials(self, mocker, shared_datadir):
        import requests
        from aws_gglog.boto3_session import Boto3Session
        from aws_gglog.greengrass_config import GreengrassConfig

        resp_mock = mocker.MagicMock()
        resp_mock.text = '{"credentials":{"accessKeyId":"123","secretAccessKey":"abc","sessionToken":"ABC","expiration":"2021-10-01T09:18:06Z"}}'
        requests.get = mocker.MagicMock(return_value=resp_mock)
        requests.raise_for_status = mocker.MagicMock()

        session_config = GreengrassConfig.parse_config(
            os.path.join(os.path.join(shared_datadir, "greengrass/config.json"))
        )
        session = Boto3Session(
            session_config,
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
