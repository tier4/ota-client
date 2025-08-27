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

import threading
from typing import Any, Generic, Iterable, Iterator, TypeVar

from typing_extensions import Self

KT = TypeVar("KT")
VT = TypeVar("VT")

DEFAULT_SET_SHARD_NUMS = 128
_NOT_SET = object()


class ShardedThreadSafeDict(Generic[KT, VT]):
    def __init__(self, num_of_shards: int = DEFAULT_SET_SHARD_NUMS) -> None:
        self._num_of_shards = num_of_shards
        self._shards: list[dict[KT, VT]] = [{} for _ in range(num_of_shards)]
        self._locks: list[threading.Lock] = [
            threading.Lock() for _ in range(num_of_shards)
        ]

    def _shard_index(self, key: KT) -> int:
        return hash(key) % self._num_of_shards

    def __contains__(self, key: KT) -> bool:
        shard_index = self._shard_index(key)
        with self._locks[shard_index]:
            return key in self._shards[shard_index]

    def __len__(self) -> int:
        return sum(len(shard) for shard in self._shards)

    def __iter__(self) -> Iterator[KT]:
        for i in range(self._num_of_shards):
            with self._locks[i]:
                yield from self._shards[i]

    def __setitem__(self, key: KT, value: VT) -> None:
        shard_index = self._shard_index(key)
        with self._locks[shard_index]:
            self._shards[shard_index][key] = value

    def __getitem__(self, key: KT) -> VT:
        shard_index = self._shard_index(key)
        with self._locks[shard_index]:
            return self._shards[shard_index][key]

    def items(self) -> Iterator[tuple[KT, VT]]:
        for i in range(self._num_of_shards):
            with self._locks[i]:
                yield from self._shards[i].items()

    keys = __iter__

    def values(self) -> Iterator[VT]:
        for i in range(self._num_of_shards):
            with self._locks[i]:
                yield from self._shards[i].values()

    def check_and_add(self, key: KT, value: VT) -> bool:
        """Check if the key is already presented, and add it if not.

        Returns:
            True is added, False if the key is already present.
        """
        shard_index = self._shard_index(key)
        with self._locks[shard_index]:
            if key in self._shards[shard_index]:
                return False
            self._shards[shard_index][key] = value
            return True

    @classmethod
    def from_iterable(
        cls,
        _in: Iterable[tuple[KT, VT]],
        *,
        num_of_shards: int = DEFAULT_SET_SHARD_NUMS,
    ) -> Self:
        instance = cls(num_of_shards)
        for key, value in _in:
            instance[key] = value
        return instance

    def pop(self, key: KT, default: VT | Any = _NOT_SET) -> VT | Any:
        shard_index = self._shard_index(key)
        with self._locks[shard_index]:
            if default is _NOT_SET:
                return self._shards[shard_index].pop(key)
            return self._shards[shard_index].pop(key, default)

    def clear(self) -> None:
        for i in range(self._num_of_shards):
            with self._locks[i]:
                self._shards[i].clear()
