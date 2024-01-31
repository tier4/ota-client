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
import time
from functools import partial, wraps
from typing import Any, Callable, Optional

from otaclient._utils.typing import RT, P


def retry(
    func: Optional[Callable[P, RT]] = None,
    /,
    backoff_factor: float = 0.1,
    backoff_max: int = 6,
    max_retry: int = 6,
    retry_on_exceptions: tuple[type[Exception], ...] = (Exception,),
) -> partial[Any] | Callable[P, RT]:
    if func is None:
        return partial(
            retry,
            backoff_factor=backoff_factor,
            backoff_max=backoff_max,
            max_retry=max_retry,
            retry_on_exceptions=retry_on_exceptions,
        )

    @wraps(func)
    def _inner(*args: P.args, **kwargs: P.kwargs) -> RT:
        _retry_count = 0
        while True:
            try:
                return func(*args, **kwargs)
            except retry_on_exceptions:
                if max_retry <= 0 or _retry_count < max_retry:
                    _sleeptime = min(backoff_factor * (2**_retry_count), backoff_max)
                    time.sleep(_sleeptime)

                    _retry_count += 1
                    continue
                raise

    return _inner
