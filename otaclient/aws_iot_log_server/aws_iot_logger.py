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


from __future__ import annotations
import logging
import time
from botocore.client import BaseClient
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from queue import Queue, Empty
from typing import Optional, cast
from pydantic import BaseModel

from otaclient.aws_iot_log_server.boto3_session import Boto3Session
from otaclient.aws_iot_log_server.greengrass_config import GreengrassConfig

from otaclient._utils.retry import retry

logger = logging.getLogger(__name__)


class LogMessage(BaseModel):
    timestamp: int
    message: str


class LogEvent(BaseModel):
    logGroupName: str
    logStreamName: str
    logEvents: list[LogMessage]
    sequenceToken: Optional[str] = None


class AwsIotLogger:
    def __init__(
        self,
        aws_credential_provider_endpoint: str,
        aws_role_alias: str,
        aws_cloudwatch_log_group: str,
        gg_config: GreengrassConfig,
        max_queue_len=4096,
        max_logs_per_merge=128,
        interval=60,
    ):
        _boto3_session = Boto3Session(
            gg_config=gg_config,
            credential_provider_endpoint=aws_credential_provider_endpoint,
            role_alias=aws_role_alias,
        )
        _client = cast(BaseClient, _boto3_session.get_session().client("logs"))

        # create log group
        @retry(max_retry=6, backoff_factor=2)
        def _create_log_group():
            try:
                _client.create_log_group(logGroupName=aws_cloudwatch_log_group)
                logger.info(f"{aws_cloudwatch_log_group=} has been created")
            except (_client.exceptions.ResourceAlreadyExistsException,):
                logger.info(
                    f"{aws_cloudwatch_log_group=} already existed, skip creating"
                )
            except Exception as e:
                logger.error(f"{aws_cloudwatch_log_group=} failed to be created: {e!r}")
                raise

        _create_log_group()

        self._log_group_name = aws_cloudwatch_log_group
        self._client = _client
        self._thing_name = gg_config.thing_name
        self._sequence_tokens = {}
        self._interval = interval
        self._log_message_queue: Queue[tuple[str, LogMessage]] = Queue(
            maxsize=max_queue_len
        )
        self._max_logs_per_merge = max_logs_per_merge
        self._executor = ThreadPoolExecutor()
        self._executor.submit(self._send_messages_thread)

    @retry(max_retry=6, backoff_factor=2)
    def send_messages(
        self,
        log_stream_suffix: str,
        message_list: list[LogMessage],
    ):
        log_stream_name = self._get_log_stream_name(log_stream_suffix)

        _client = self._client
        try:
            response = _client.put_log_events(
                **LogEvent(
                    logGroupName=self._log_group_name,
                    logStreamName=log_stream_name,
                    logEvents=message_list,
                    sequenceToken=self._sequence_tokens.get(log_stream_name),
                ).model_dump(exclude_none=True)
            )
            if _sequence_token := response.get("nextSequenceToken"):
                self._sequence_tokens[log_stream_name] = _sequence_token
        except (
            _client.exceptions.DataAlreadyAcceptedException,
            _client.exceptions.InvalidSequenceTokenException,
        ) as e:
            next_expected_token = e.response["Error"]["Message"].rsplit(" ", 1)[-1]
            # null as the next sequenceToken means don't include any
            # sequenceToken at all, not that the token should be set to "null"
            if next_expected_token == "null":
                self._sequence_tokens.pop(log_stream_name, None)
            else:
                self._sequence_tokens[log_stream_name] = next_expected_token
        except _client.exceptions.ResourceNotFoundException as e:
            try:
                _client.create_log_stream(
                    logGroupName=self._log_group_name,
                    logStreamName=log_stream_name,
                )
                logger.info(f"{log_stream_name=} has been created.")
            except (
                _client.exceptions.OperationAbortedException,
                _client.exceptions.ResourceAlreadyExistsException,
            ):
                self._sequence_tokens.pop(log_stream_name, None)
            raise
        except Exception as e:
            logger.error(
                f"put_log_events failure: {e!r}"
                f"log_group_name={self._log_group_name}, "
                f"log_stream_name={log_stream_name}"
            )
            # just ignore other unhandled exceptions

    def put_message(self, log_stream_suffix: str, message: LogMessage):
        _queue = self._log_message_queue
        with _queue.mutex:
            if _queue.maxsize > 0 and _queue._qsize() >= _queue.maxsize:
                _queue.get_nowait()  # drop one oldest entry
            _queue.put((log_stream_suffix, message))

    def _send_messages_thread(self):
        while True:
            # merge message
            message_dict: dict[str, list[LogMessage]] = {}

            _merge_count = 0
            while _merge_count < self._max_logs_per_merge:
                _queue = self._log_message_queue
                try:
                    data = _queue.get_nowait()
                    _merge_count += 1
                    log_stream_suffix, message = data

                    if log_stream_suffix not in message_dict:
                        message_dict[log_stream_suffix] = []
                    message_dict[log_stream_suffix].append(message)
                except Empty:
                    break

            # send merged message
            for log_stream_suffix, logs in message_dict.items():
                self.send_messages(log_stream_suffix, logs)

            time.sleep(self._interval)

    def _get_log_stream_name(self, log_stream_sufix):
        fmt = "{strftime:%Y/%m/%d}".format(strftime=datetime.utcnow())
        return f"{fmt}/{self._thing_name}/{log_stream_sufix}"
