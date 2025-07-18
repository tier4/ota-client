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
"""A lib for sharing memory between processes.

shared memory layout:

rwlock(1byte) | hmac-sha512 of msg(64bytes) | msg_len(4bytes,big) | msg(<msg_len>bytes)
In which, msg is pickled python object.
"""


from __future__ import annotations

import hashlib
import hmac
import logging
import multiprocessing.shared_memory as mp_shm
import pickle
import time
from typing import Generic

from otaclient_common._typing import T

logger = logging.getLogger(__name__)

DEFAULT_HASH_ALG = "sha512"
DEFAULT_KEY_LEN = hashlib.new(DEFAULT_HASH_ALG).digest_size

RWLOCK_LEN = 1  # byte
PAYLOAD_LEN_BYTES = 4  # bytes

RWLOCK_LOCKED = b"\xab"
RWLOCK_OPEN = b"\x54"


class RWBusy(Exception): ...


class SHA512Verifier:
    """Base class for specifying hash alg related configurations."""

    DIGEST_ALG = "sha512"
    DIGEST_SIZE = hashlib.new(DIGEST_ALG).digest_size
    MIN_ENCAP_MSG_LEN = RWLOCK_LEN + DIGEST_SIZE + PAYLOAD_LEN_BYTES

    _key: bytes

    def cal_hmac(self, _raw_msg: bytes) -> bytes:
        return hmac.digest(key=self._key, msg=_raw_msg, digest=self.DIGEST_ALG)

    def verify_msg(self, _raw_msg: bytes, _expected_hmac: bytes) -> bool:
        return hmac.compare_digest(
            hmac.digest(
                key=self._key,
                msg=_raw_msg,
                digest=self.DIGEST_ALG,
            ),
            _expected_hmac,
        )


def _ensure_connect_shm(
    name: str, *, max_retry: int, retry_interval: int
) -> mp_shm.SharedMemory:
    for _idx in range(max_retry):
        try:
            return mp_shm.SharedMemory(name=name, create=False)
        except Exception as e:
            logger.warning(
                f"retry #{_idx}: failed to connect to {name=}: {e!r}, keep retrying ..."
            )
            time.sleep(retry_interval)
    raise ValueError(f"failed to connect share memory with {name=}")


class MPSharedMemoryReader(SHA512Verifier, Generic[T]):

    def __init__(
        self,
        *,
        name: str,
        key: bytes,
        max_retry: int = 6,
        retry_interval: int = 1,
    ) -> None:
        self._shm = shm = _ensure_connect_shm(
            name, max_retry=max_retry, retry_interval=retry_interval
        )
        self.mem_size = size = shm.size
        self.msg_max_size = size - self.MIN_ENCAP_MSG_LEN
        self._key = key

    def atexit(self) -> None:
        self._shm.close()

    def sync_msg(self) -> T:
        """Get msg from shared memory.

        Raises:
            RWBusy if rwlock indicates the writer is writing or not yet ready.
                ValueError for invalid msg.
        """
        buffer = self._shm.buf

        # check if we can read
        _cursor = 0
        rwlock = bytes(buffer[_cursor:RWLOCK_LEN])
        if rwlock != RWLOCK_OPEN:
            if rwlock == RWLOCK_LOCKED:
                raise RWBusy("write in progress, abort")
            raise RWBusy("no msg has been written yet")
        _cursor += RWLOCK_LEN

        # parsing the msg
        input_hmac = bytes(buffer[_cursor : _cursor + self.DIGEST_SIZE])
        _cursor += self.DIGEST_SIZE

        _payload_len_bytes = bytes(buffer[_cursor : _cursor + PAYLOAD_LEN_BYTES])
        payload_len = int.from_bytes(_payload_len_bytes, "big", signed=False)
        _cursor += PAYLOAD_LEN_BYTES

        if payload_len > self.msg_max_size:
            raise ValueError(f"invalid msg: {payload_len=} > {self.msg_max_size}")

        payload = bytes(buffer[_cursor : _cursor + payload_len])
        if self.verify_msg(payload, input_hmac):
            return pickle.loads(payload)
        raise ValueError("failed to validate input msg")


class MPSharedMemoryWriter(SHA512Verifier, Generic[T]):

    def __init__(
        self,
        *,
        name: str | None = None,
        size: int = 0,
        key: bytes,
        create: bool = False,
        msg_max_size: int | None = None,
        max_retry: int = 6,
        retry_interval: int = 1,
    ) -> None:
        if create:
            _msg_max_size = size - self.MIN_ENCAP_MSG_LEN
            if _msg_max_size < 0:
                raise ValueError(f"{size=} < {self.MIN_ENCAP_MSG_LEN=}")
            self._shm = shm = mp_shm.SharedMemory(name=name, size=size, create=True)
            self.mem_size = shm.size

        elif name:
            self._shm = shm = _ensure_connect_shm(
                name, max_retry=max_retry, retry_interval=retry_interval
            )
            self.mem_size = size = shm.size
            _msg_max_size = size - self.MIN_ENCAP_MSG_LEN
            if _msg_max_size < 0:
                shm.close()
                raise ValueError(f"{size=} < {self.MIN_ENCAP_MSG_LEN=}")
        else:
            raise ValueError("<name> must be specified if <create> is False")

        self.name = shm.name
        self._key = key
        self.msg_max_size = min(_msg_max_size, msg_max_size or float("infinity"))

    def atexit(self) -> None:
        self._shm.close()

    def write_msg(self, obj: T) -> None:
        """Write msg to shared memory.

        Raises:
            ValueError on invalid msg or exceeding shared memory size.
        """
        buffer = self._shm.buf
        _pickled = pickle.dumps(obj)
        _pickled_len = len(_pickled)

        if _pickled_len > self.msg_max_size:
            raise ValueError(f"exceed {self.msg_max_size=}: {_pickled_len=}")

        msg = b"".join(
            [
                RWLOCK_LOCKED,
                self.cal_hmac(_pickled),
                _pickled_len.to_bytes(PAYLOAD_LEN_BYTES, "big", signed=False),
                _pickled,
            ]
        )
        msg_len = len(msg)
        if msg_len > self.mem_size:
            raise ValueError(f"{msg_len=} > {self.mem_size=}")

        buffer[:msg_len] = msg
        buffer[:1] = RWLOCK_OPEN
