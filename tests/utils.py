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


import asyncio
import os
import time
import zstandard
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Union
from functools import partial
from pathlib import Path


import grpc
from otaclient.app.common import file_sha256
from otaclient.app.proto import otaclient_v2_pb2_grpc as v2_grpc, wrapper

import logging

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
                raise ValueError(f"{_path}")
        elif _a_path.is_dir():
            if not _b_path.is_dir():
                raise ValueError(f"{_path}")

        elif _a_path.is_file():
            if not (_b_path.is_file() and file_sha256(_a_path) == file_sha256(_b_path)):
                raise ValueError(f"{_path}")
        else:
            raise ValueError(f"{_path}")


class DummySubECU:
    SUCCESS_RESPONSE = wrapper.v2.Status(
        status=wrapper.StatusOta.SUCCESS.value,
        failure=wrapper.FailureType.NO_FAILURE.value,
    )
    UPDATING_RESPONSE = wrapper.v2.Status(
        status=wrapper.StatusOta.UPDATING.value,
        failure=wrapper.FailureType.NO_FAILURE.value,
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
            res = wrapper.StatusResponse(
                ecu=[
                    wrapper.v2.StatusResponseEcu(
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
            res = wrapper.StatusResponse(
                ecu=[
                    wrapper.v2.StatusResponseEcu(
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
        res = wrapper.StatusResponse(
            ecu=[
                wrapper.v2.StatusResponseEcu(
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
