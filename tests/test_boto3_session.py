class TestBoto3Session:
    def test_parse_config(self):
        from boto3_session import Boto3Session

        config = Boto3Session.parse_config("./testdata/greengrass/config.json")
        assert config.get("ca_cert") == "/greengrass/certs/root.ca.pem"
        assert config.get("private_key") == "/greengrass/certs/gg.private.key"
        assert config.get("cert") == "/greengrass/certs/gg.cert.pem"
        assert config.get("region") == "ap-northeast-1"
        assert config.get("thing_name") == "foo-bar"

    def test__refresh_credentials(self, mocker):
        import requests
        from boto3_session import Boto3Session

        resp_mock = mocker.MagicMock()
        resp_mock.text = '{"credentials":{"accessKeyId":"123","secretAccessKey":"abc","sessionToken":"ABC","expiration":"2021-10-01T09:18:06Z"}}'
        requests.get = mocker.MagicMock(return_value=resp_mock)
        requests.raise_for_status = mocker.MagicMock()

        session = Boto3Session("./testdata/greengrass/config.json", "https://example.com", "example_role_alias")
        got_credential = session._refresh_credentials()
        want_credential = {
            "access_key": "123",
            "secret_key": "abc",
            "token": "ABC",
            "expiry_time": "2021-10-01T09:18:06Z",
        }

        assert got_credential == want_credential
