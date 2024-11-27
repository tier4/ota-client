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

import multiprocessing as mp
import multiprocessing.shared_memory as mp_shm
import multiprocessing.synchronize as mp_sync
import secrets
import time
from dataclasses import dataclass
from functools import partial

import pytest

from otaclient_common.shm_status import (
    DEFAULT_KEY_LEN,
    MPSharedStatusReader,
    MPSharedStatusWriter,
    RWBusy,
)


@dataclass
class OuterMsg:
    _inner_msg: InnerMsg


@dataclass
class InnerMsg:
    i_int: int
    i_str: str


class MsgReader(MPSharedStatusReader[OuterMsg]): ...


class MsgWriter(MPSharedStatusWriter[OuterMsg]): ...


DATA_ENTRIES_NUM = 10
_TEST_DATA = {
    _idx: OuterMsg(
        InnerMsg(
            i_int=_idx,
            i_str=str(_idx),
        )
    )
    for _idx in range(DATA_ENTRIES_NUM)
}
SHM_SIZE = 1024


def writer_process(
    shm_name: str,
    key: bytes,
    *,
    interval: float,
    write_all_flag: mp_sync.Event,
):
    _shm_writer = MsgWriter(name=shm_name, key=key)

    for _, _entry in _TEST_DATA.items():
        _shm_writer.write_msg(_entry)
        time.sleep(interval)
    write_all_flag.set()


def read_slow_process(
    shm_name: str,
    key: bytes,
    *,
    interval: float,
    success_flag: mp_sync.Event,
):
    """Reader is slower than writer, we only need to ensure reader can read the latest written msg."""
    _shm_reader = MsgReader(name=shm_name, key=key)

    while True:
        _msg = _shm_reader.sync_msg()

        if _msg._inner_msg.i_int == DATA_ENTRIES_NUM - 1:
            return success_flag.set()
        time.sleep(interval)


def read_fast_process(
    shm_name: str,
    key: bytes,
    *,
    interval: float,
    success_flag: mp_sync.Event,
):
    """Reader is faster than writer, we need to ensure all the msgs are read."""
    _shm_reader = MsgReader(name=shm_name, key=key)
    _read = [False for _ in range(DATA_ENTRIES_NUM)]

    while True:
        time.sleep(interval)
        try:
            _msg = _shm_reader.sync_msg()
        except RWBusy:
            continue

        _read[_msg._inner_msg.i_int] = True
        if all(_read):
            return success_flag.set()


WRITE_INTERVAL = 0.1
READ_SLOW_INTERVAL = 0.5
READ_FAST_INTERVAL = 0.01


@pytest.mark.parametrize(
    "reader_func, read_interval, timeout",
    (
        (
            read_fast_process,
            READ_FAST_INTERVAL,
            WRITE_INTERVAL * DATA_ENTRIES_NUM + 1,
        ),
        (
            read_slow_process,
            READ_SLOW_INTERVAL,
            WRITE_INTERVAL * DATA_ENTRIES_NUM + 1,
        ),
    ),
)
def test_shm_status_read_fast(reader_func, read_interval, timeout):
    _shm = mp_shm.SharedMemory(size=SHM_SIZE, create=True)
    _mp_ctx = mp.get_context("spawn")
    _key = secrets.token_bytes(DEFAULT_KEY_LEN)

    _write_all_flag = _mp_ctx.Event()
    _success_flag = _mp_ctx.Event()

    _writer_p = _mp_ctx.Process(
        target=partial(
            writer_process,
            shm_name=_shm.name,
            key=_key,
            interval=WRITE_INTERVAL,
            write_all_flag=_write_all_flag,
        )
    )
    _reader_p = _mp_ctx.Process(
        target=partial(
            reader_func,
            shm_name=_shm.name,
            key=_key,
            interval=read_interval,
            success_flag=_success_flag,
        )
    )
    _writer_p.start()
    _reader_p.start()

    time.sleep(timeout)
    try:
        assert _write_all_flag.is_set(), "writer timeout finish up writing"
        assert _success_flag.is_set(), "reader failed to read all msg"
    finally:
        _writer_p.terminate()
        _writer_p.join()

        _reader_p.terminate()
        _reader_p.join()

        _shm.close()
        _shm.unlink()
