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

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Union

import grpc
import zstandard
from google.protobuf.message import Message as _Message

from otaclient_api.v2 import otaclient_v2_pb2_grpc as v2_grpc
from otaclient_api.v2 import types as api_types
from otaclient_common.common import file_sha256

logger = logging.getLogger(__name__)


@dataclass
class SlotMeta:
    """
    NOTE: For test setup convenience, even for grub controller scheme that
        doesn't use separate boot dev, we still simluate a separate boot dev.

        For grub controller, we use <boot_dev>/boot/ota-status as ota-partition folder,
        and use a general boot dir to store ota-partition files.
        For cboot controller, we use <boot_dev> directly.
    """

    slot_a: str
    slot_b: str
    slot_a_boot_dev: str
    slot_b_boot_dev: str


@asynccontextmanager
async def run_otaclient_server(otaclient_service_v2, listen_addr):
    server = grpc.aio.server()
    v2_grpc.add_OtaClientServiceServicer_to_server(
        otaclient_service_v2,
        server,
    )

    server.add_insecure_port(listen_addr)
    background_task = asyncio.create_task(server.start())
    try:
        yield
    finally:
        await server.stop(None)
        background_task.cancel()  # ensure the task termination


def run_http_server(addr: str, port: int, *, directory: str):
    import http.server as http_server

    def _dummy_logger(*args, **kwargs):
        return

    http_server.SimpleHTTPRequestHandler.log_message = _dummy_logger

    handler_class = partial(http_server.SimpleHTTPRequestHandler, directory=directory)
    with http_server.ThreadingHTTPServer((addr, port), handler_class) as httpd:
        httpd.serve_forever()


def compare_dir(left: Path, right: Path):
    _a_glob = set(map(lambda x: x.relative_to(left), left.glob("**/*")))
    _b_glob = set(map(lambda x: x.relative_to(right), right.glob("**/*")))
    if not _a_glob == _b_glob:  # first check paths are identical
        raise ValueError(
            f"left and right mismatch, diff: {_a_glob.symmetric_difference(_b_glob)}\n"
            f"{_a_glob=}\n"
            f"{_b_glob=}"
        )

    # then check each file/folder of the path
    # NOTE/TODO: stats is not checked
    for _path in _a_glob:
        _a_path = left / _path
        _b_path = right / _path
        if _a_path.is_symlink():
            if not (
                _b_path.is_symlink() and os.readlink(_a_path) == os.readlink(_b_path)
            ):
                raise ValueError(f"symlink mismatched: {_path}")
        elif _a_path.is_dir():
            if not _b_path.is_dir():
                raise ValueError(f"dir mismatched: {_path}")

        elif _a_path.is_file():
            if not (_b_path.is_file() and file_sha256(_a_path) == file_sha256(_b_path)):
                logger.error(f"{_a_path.read_text()=}, {_b_path.read_text()=}")
                raise ValueError(f"file check failed: {_path}")
        else:
            raise ValueError(f"unspecific file type: {_path}")


class DummySubECU:
    SUCCESS_RESPONSE = api_types.Status(
        status=api_types.StatusOta.SUCCESS,
        failure=api_types.FailureType.NO_FAILURE,
    )
    UPDATING_RESPONSE = api_types.Status(
        status=api_types.StatusOta.UPDATING,
        failure=api_types.FailureType.NO_FAILURE,
    )
    UPDATE_TIME_COST = 6
    REBOOT_TIME_COST = 1

    def __init__(self, ecu_id) -> None:
        self._receive_update_time = None
        self._update_succeeded = False
        self.ecu_id = ecu_id

    def start(self):
        logger.debug(f"dummy subecu: start update at {time.time()=}")
        self._receive_update_time = time.time()

    def status(self):
        logger.debug(f"{self.ecu_id=}, status API called...")
        # update not yet started
        if self._receive_update_time is None:
            logger.debug(f"{self.ecu_id=}, update not yet started")
            res = api_types.StatusResponse(
                ecu=[
                    api_types.StatusResponseEcu(
                        ecu_id=self.ecu_id,
                        status=self.SUCCESS_RESPONSE,
                    )
                ],
                available_ecu_ids=[self.ecu_id],
            )
            return res
        # update finished
        if time.time() >= (
            self._receive_update_time + self.UPDATE_TIME_COST + self.REBOOT_TIME_COST
        ):
            logger.debug(
                f"update finished for {self.ecu_id=}, {self._receive_update_time=}, {time.time()=}"
            )
            res = api_types.StatusResponse(
                ecu=[
                    api_types.StatusResponseEcu(
                        ecu_id=self.ecu_id,
                        status=self.SUCCESS_RESPONSE,
                    )
                ],
                available_ecu_ids=[self.ecu_id],
            )
            self._update_succeeded = True
            return res
        # rebooting
        if time.time() >= (self._receive_update_time + self.UPDATE_TIME_COST):
            logger.debug(f"{self.ecu_id=}, rebooting")
            return None
        # updating
        logger.debug(f"{self.ecu_id=}, updating")
        res = api_types.StatusResponse(
            ecu=[
                api_types.StatusResponseEcu(
                    ecu_id=self.ecu_id,
                    status=self.UPDATING_RESPONSE,
                )
            ],
            available_ecu_ids=[self.ecu_id],
        )
        return res


def zstd_compress_file(src: Union[str, Path], dst: Union[str, Path]):
    cctx = zstandard.ZstdCompressor()
    with open(src, "rb") as src_f, open(dst, "wb") as dst_f:
        cctx.copy_stream(src_f, dst_f)


def compare_message(l, r):
    """
    NOTE: we don't directly compare two protobuf message by ==
          due to the behavior difference between empty Duration and
          unset Duration.
    """
    if (_proto_class := type(l)) is not type(r):
        raise TypeError(f"{type(l)=} != {type(r)=}")

    for _attrn in _proto_class.__slots__:
        _attrv_l, _attrv_r = getattr(l, _attrn), getattr(r, _attrn)
        # first check each corresponding attr has the same type,
        assert type(_attrv_l) == type(_attrv_r), f"compare failed on {_attrn=}"

        if isinstance(_attrv_l, _Message):
            compare_message(_attrv_l, _attrv_r)
        else:
            assert _attrv_l == _attrv_r, f"mismatch {_attrv_l=}, {_attrv_r=}"
