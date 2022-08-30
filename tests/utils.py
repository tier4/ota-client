import asyncio
import os
import time
import http.server as http_server
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import grpc
from app.common import file_sha256
from app.proto import otaclient_v2_pb2_grpc as v2_grpc, wrapper


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
    UPDATE_TIME_COST = 3
    REBOOT_TIME_COST = 1

    def __init__(self, ecu_id) -> None:
        self._receive_update_time = None
        self.ecu_id = ecu_id

    def start(self):
        self._receive_update_request = time.time()

    def status(self):
        # update not yet started
        if self._receive_update_time is None:
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
        if (
            self._receive_update_time + self.UPDATE_TIME_COST + self.REBOOT_TIME_COST
        ) >= time.time():
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
        # rebooting
        if (self._receive_update_time + self.UPDATE_TIME_COST) >= time.time():
            return None
        # updating
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
