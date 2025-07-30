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

import itertools
import threading
from typing import Dict, Generic, Iterable, Iterator, TypeVar

from typing_extensions import Self

T = TypeVar("T")
KT = TypeVar("KT")
VT = TypeVar("VT")

DEFAULT_SET_SHARD_NUMS = 128


class ShardedThreadSafeSet(Generic[T]):
    def __init__(self, num_of_shards: int = DEFAULT_SET_SHARD_NUMS) -> None:
        self._num_of_shards = num_of_shards
        self._shards: list[set[T]] = [set() for _ in range(num_of_shards)]
        self._locks: list[threading.Lock] = [
            threading.Lock() for _ in range(num_of_shards)
        ]

    def __contains__(self, key: T) -> bool:
        shard_index = self._shard_index(key)
        with self._locks[shard_index]:
            return key in self._shards[shard_index]

    def __len__(self) -> int:
        return sum(len(shard) for shard in self._shards)

    def __iter__(self) -> Iterator[T]:
        return itertools.chain(*self._shards)

    def _shard_index(self, key: T) -> int:
        return hash(key) % self._num_of_shards

    def add(self, key: T) -> None:
        shard_index = self._shard_index(key)
        with self._locks[shard_index]:
            self._shards[shard_index].add(key)

    def check_and_add(self, key: T) -> bool:
        """Check if the key is already in the set, and add it if not.

        Returns:
            True is added, False if the key is already present.
        """
        shard_index = self._shard_index(key)
        with self._locks[shard_index]:
            if key in self._shards[shard_index]:
                return False
            self._shards[shard_index].add(key)
            return True

    @classmethod
    def from_iterable(
        cls, _in: Iterable[T], *, num_of_shards: int = DEFAULT_SET_SHARD_NUMS
    ) -> Self:
        instance = cls(num_of_shards)
        for item in _in:
            instance.add(item)
        return instance

    def remove(self, key: T) -> None:
        shard_index = self._shard_index(key)
        with self._locks[shard_index]:
            self._shards[shard_index].remove(key)

    def discard(self, key: T) -> None:
        shard_index = self._shard_index(key)
        with self._locks[shard_index]:
            self._shards[shard_index].discard(key)

    def clear(self) -> None:
        for i in range(self._num_of_shards):
            with self._locks[i]:
                self._shards[i].clear()


class ThreadSafeDict(Dict[KT, VT]):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._lock = threading.Lock()

    def __enter__(self) -> Self:
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._lock.release()
        return False
