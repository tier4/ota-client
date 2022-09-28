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


from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer

from .aws_iot_logger import AwsIotLogger
from .http_server import HttpHandler


def sender(
    aws_credential_provider_endpoint,
    aws_role_alias,
    aws_cloudwatch_log_group,
    ca_cert_file,
    private_key_file,
    cert_file,
    region,
    thing_name,
    interval=4,
):
    iot_logger = AwsIotLogger(
        aws_credential_provider_endpoint,
        aws_role_alias,
        aws_cloudwatch_log_group,
        ca_cert_file,
        private_key_file,
        cert_file,
        region,
        thing_name,
        interval,
    )

    while True:
        try:
            data = HttpHandler._queue.get()
            message = {"timestamp": data["timestamp"], "message": data["message"]}
            log_stream_suffix = "/".join(data["path"])
            iot_logger.put_message(log_stream_suffix, message)
        except Exception as e:
            print(e)


def launch_server(
    host: str,
    port: int,
    aws_credential_provider_endpoint,
    aws_role_alias,
    aws_cloudwatch_log_group,
    ca_cert_file,
    private_key_file,
    cert_file,
    region,
    thing_name,
):
    server = HTTPServer((host, port), HttpHandler)
    with ThreadPoolExecutor() as executor:
        executor.submit(
            sender,
            aws_credential_provider_endpoint,
            aws_role_alias,
            aws_cloudwatch_log_group,
            ca_cert_file,
            private_key_file,
            cert_file,
            region,
            thing_name,
        )
        server.serve_forever()
