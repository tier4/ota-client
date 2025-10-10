from typing import Any, Iterable


def iter_helper(_iter: Iterable[Any]) -> int:
    _count = 0
    for _count, _ in enumerate(_iter, start=1):
        ...
    return _count
