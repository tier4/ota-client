import time
from retrying import retry
from botocore.exceptions import ClientError
from datetime import datetime
import logging
from queue import Queue, Empty
from concurrent.futures import ThreadPoolExecutor
from typing import TypedDict, List

from boto3_session import Boto3Session
from configs import LOG_FORMAT

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
_sh = logging.StreamHandler()
fmt = logging.Formatter(fmt=LOG_FORMAT)
_sh.setFormatter(fmt)
logger.addHandler(_sh)


class LogMessage(TypedDict):
    timestamp: int
    message: str


class AwsIotLogger:
    def __init__(
        self,
        aws_credential_provider_endpoint,
        aws_role_alias,
        aws_cloudwatch_log_group,
        ca_cert_file,
        private_key_file,
        cert_file,
        region,
        thing_name,
        interval=60,
    ):
        _boto3_session = Boto3Session(
            config={
                "ca_cert": ca_cert_file,
                "cert": cert_file,
                "private_key": private_key_file,
                "region": region,
                "thing_name": thing_name,
            },
            credential_provider_endpoint=aws_credential_provider_endpoint,
            role_alias=aws_role_alias,
        )
        _client = _boto3_session.get_session().client("logs")

        # create log group
        try:
            _client.create_log_group(logGroupName=aws_cloudwatch_log_group)
            logger.info(f"{aws_cloudwatch_log_group=} has been created.")
        except (
            _client.exceptions.OperationAbortedException,
            _client.exceptions.ResourceAlreadyExistsException,
        ):
            pass
        self._log_group_name = aws_cloudwatch_log_group
        self._client = _client
        self._thing_name = thing_name
        self._sequence_tokens = {}
        self._interval = interval
        self._log_message_queue = Queue()
        self._executor = ThreadPoolExecutor()
        self._executor.submit(self._send_messages_thread)

    @retry(stop_max_attempt_number=5, wait_fixed=500)
    def send_messages(
        self,
        log_stream_suffix: str,
        message_list: List[LogMessage],
    ):
        log_stream_name = self._get_log_stream_name(log_stream_suffix)
        sequence_token = self._sequence_tokens.get(log_stream_name)
        _client = self._client
        try:
            log_event = {
                "logGroupName": self._log_group_name,
                "logStreamName": log_stream_name,
                "logEvents": message_list,
            }
            if sequence_token:
                log_event["sequenceToken"] = sequence_token

            response = _client.put_log_events(**log_event)
            self._sequence_tokens[log_stream_name] = response.get("nextSequenceToken")
        except ClientError as e:
            if isinstance(
                e,
                (
                    _client.exceptions.DataAlreadyAcceptedException,
                    _client.exceptions.InvalidSequenceTokenException,
                ),
            ):
                next_expected_token = e.response["Error"]["Message"].rsplit(" ", 1)[-1]
                # null as the next sequenceToken means don't include any
                # sequenceToken at all, not that the token should be set to "null"
                if next_expected_token == "null":
                    self._sequence_tokens[log_stream_name] = None
                else:
                    self._sequence_tokens[log_stream_name] = next_expected_token
            elif isinstance(e, _client.exceptions.ResourceNotFoundException):
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
                    self._sequence_tokens[log_stream_name] = None
            raise
        except Exception:
            # put log and just ignore
            logger.exception(
                "put_log_events failure: "
                f"log_group_name={self._log_group_name}, "
                f"log_stream_name={log_stream_name}"
            )

    def put_message(self, log_stream_suffix: str, message: LogMessage):
        data = {log_stream_suffix: message}
        self._log_message_queue.put(data)

    def _send_messages_thread(self):
        try:
            while True:
                # merge message
                message_dict = {}
                while True:
                    try:
                        q = self._log_message_queue
                        data = q.get(block=False)
                        log_stream_suffix = next(iter(data))  # first key
                        if log_stream_suffix not in message_dict:
                            message_dict[log_stream_suffix] = []
                        message = data[log_stream_suffix]
                        message_dict[log_stream_suffix].append(message)
                    except Empty:
                        break
                # send merged message
                for k, v in message_dict.items():
                    self.send_messages(k, v)
                time.sleep(self._interval)
        except Exception as e:
            logger.exception(e)

    def _get_log_stream_name(self, log_stream_sufix):
        fmt = "{strftime:%Y/%m/%d}".format(strftime=datetime.utcnow())
        return f"{fmt}/{self._thing_name}/{log_stream_sufix}"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--aws_credential_provider_endpoint", required=True)
    parser.add_argument("--aws_role_alias", required=True)
    parser.add_argument("--aws_cloudwatch_log_group", required=True)
    parser.add_argument("--ca_cert_file")
    parser.add_argument("--private_key_file")
    parser.add_argument("--cert_file")
    parser.add_argument("--region")
    parser.add_argument("--thing_name")
    args = parser.parse_args()

    kwargs = dict(
        aws_credential_provider_endpoint=args.aws_credential_provider_endpoint,
        aws_role_alias=args.aws_role_alias,
        aws_cloudwatch_log_group=args.aws_cloudwatch_log_group,
        ca_cert_file=args.ca_cert_file,
        private_key_file=args.private_key_file,
        cert_file=args.cert_file,
        region=args.region,
        thing_name=args.thing_name,
        interval=2,
    )

    aws_iot_logger = AwsIotLogger(**kwargs)

    def create_log_message(message: str):
        return {"timestamp": int(time.time()) * 1000, "message": message}

    aws_iot_logger.send_messages("my_ecu_name4", [create_log_message("hello")])
    aws_iot_logger.send_messages("my_ecu_name4", [create_log_message("hello-x")])
    aws_iot_logger.send_messages("my_ecu_name4", [create_log_message("hello-xx")])

    aws_iot_logger.put_message("my_ecu_name4", create_log_message("hello-1"))
    aws_iot_logger.put_message("my_ecu_name4", create_log_message("hello-2"))
    aws_iot_logger.put_message("my_ecu_name4", create_log_message("hello-3"))
