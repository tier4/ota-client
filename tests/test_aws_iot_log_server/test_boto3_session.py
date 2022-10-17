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


class TestBoto3Session:
    def test__refresh_credentials(self, mocker, shared_datadir):
        import pycurl
        import requests
        from otaclient.aws_iot_log_server.boto3_session import Boto3Session
        from otaclient.aws_iot_log_server.greengrass_config import GreengrassConfig

        response = '{"credentials":{"accessKeyId":"123","secretAccessKey":"abc","sessionToken":"ABC","expiration":"2021-10-01T09:18:06Z"}}'

        connection_mock = mocker.MagicMock()
        connection_mock.perform_rs = mocker.MagicMock(return_value=response)
        connection_mock.getinfo = mocker.MagicMock(return_value=200)
        pycurl.Curl = mocker.MagicMock(return_value=connection_mock)

        resp_mock = mocker.MagicMock()
        resp_mock.text = response

        requests.get = mocker.MagicMock(return_value=resp_mock)
        requests.raise_for_status = mocker.MagicMock()

        session_config = GreengrassConfig.parse_config(
            os.path.join(os.path.join(shared_datadir, "greengrass/config.json")),
            None,
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
