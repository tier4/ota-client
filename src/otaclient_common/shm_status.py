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
"""A lib for sharing status between processes.

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

from otaclient_common.typing import T

logger = logging.getLogger(__name__)

DEFAULT_HASH_ALG = "sha512"
DEFAULT_KEY_LEN = hashlib.new(DEFAULT_HASH_ALG).digest_size

RWLOCK_LEN = 1  # byte
PAYLOAD_LEN_BYTES = 4  # bytes

RWLOCK_LOCKED = b"\xab"
RWLOCK_OPEN = b"\x54"


class SHA512Verifier:
    """Base class for specifying hash alg related configurations."""

    digest_alg = DEFAULT_HASH_ALG
    digest_size = hashlib.new(digest_alg).digest_size
    min_encap_msg_len = RWLOCK_LEN + digest_size + PAYLOAD_LEN_BYTES


class MPSharedStatusReader(SHA512Verifier, Generic[T]):

    def __init__(
        self,
        *,
        name: str,
        key: bytes,
        max_retry: int = 6,
        retry_interval: int = 1,
    ) -> None:
        for _idx in range(max_retry):
            try:
                self._shm = shm = mp_shm.SharedMemory(name=name, create=False)
                break
            except Exception as e:
                logger.warning(
                    f"retry #{_idx}: failed to connect to {name=}: {e!r}, keep retrying ..."
                )
                time.sleep(retry_interval)
        else:
            raise ValueError(f"failed to connect share memory with {name=}")

        self.mem_size = size = shm.size
        self.msg_max_size = size - self.min_encap_msg_len
        self._key = key

    def atexit(self) -> None:
        self._shm.close()

    def sync_msg(self) -> T:
        buffer = self._shm.buf

        # check if we can read
        _cursor = 0
        rwlock = bytes(buffer[_cursor:RWLOCK_LEN])
        if rwlock != RWLOCK_OPEN:
            if rwlock == RWLOCK_LOCKED:
                raise ValueError("write in progress, abort")
            raise ValueError(f"invalid input_msg: wrong rwlock bytes: {rwlock=}")
        _cursor += RWLOCK_LEN

        # parsing the msg
        input_hmac = bytes(buffer[_cursor : _cursor + self.digest_size])
        _cursor += self.digest_size

        _payload_len_bytes = bytes(buffer[_cursor : _cursor + PAYLOAD_LEN_BYTES])
        payload_len = int.from_bytes(_payload_len_bytes, "big", signed=False)
        _cursor += PAYLOAD_LEN_BYTES

        if payload_len > self.msg_max_size:
            raise ValueError(f"invalid msg: {payload_len=} > {self.msg_max_size}")

        payload = bytes(buffer[_cursor : _cursor + payload_len])
        payload_hmac = hmac.digest(key=self._key, msg=payload, digest=self.digest_alg)

        if hmac.compare_digest(payload_hmac, input_hmac):
            return pickle.loads(payload)
        raise ValueError("failed to validate input msg")


class MPSharedStatusWriter(SHA512Verifier, Generic[T]):

    def __init__(
        self,
        *,
        name: str | None = None,
        size: int = 0,
        create: bool = False,
        msg_max_size: int | None = None,
        key: bytes,
    ) -> None:
        if create:
            _msg_max_size = size - self.min_encap_msg_len
            if _msg_max_size < 0:
                raise ValueError(f"{size=} < {self.min_encap_msg_len=}")
            self._shm = shm = mp_shm.SharedMemory(name=name, size=size, create=True)
            self.mem_size = shm.size
        else:
            self._shm = shm = mp_shm.SharedMemory(name=name, create=False)
            self.mem_size = size = shm.size
            _msg_max_size = size - self.min_encap_msg_len
            if _msg_max_size < 0:
                shm.close()
                raise ValueError(f"{size=} < {self.min_encap_msg_len=}")

        self.name = shm.name
        self._key = key
        self.msg_max_size = min(_msg_max_size, msg_max_size or float("infinity"))

    def atexit(self) -> None:
        self._shm.close()

    def write_msg(self, obj: T) -> None:
        buffer = self._shm.buf
        _pickled = pickle.dumps(obj)
        _pickled_len = len(_pickled)

        if _pickled_len > self.msg_max_size:
            raise ValueError(f"exceed {self.msg_max_size=}: {_pickled_len=}")

        _hmac = hmac.digest(key=self._key, msg=_pickled, digest=self.digest_alg)
        msg = b"".join(
            [
                RWLOCK_LOCKED,
                _hmac,
                _pickled_len.to_bytes(PAYLOAD_LEN_BYTES, "big", signed=False),
                _pickled,
            ]
        )
        msg_len = len(msg)
        if msg_len > self.mem_size:
            raise ValueError(f"{msg_len=} > {self.mem_size=}")

        buffer[:msg_len] = msg
        buffer[:1] = RWLOCK_OPEN
