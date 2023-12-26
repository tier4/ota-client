from __future__ import annotations
from .typing import StrOrPath

_SPLITTER = ":"


class ParsedPasswd:
    """Parse passwd and store name/uid mapping.

    Example passwd entry line:
    nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin

    Attrs:
        _by_name (dict[str, int]): name:uid mapping.
        _by_uid (dict[int, str]): uid:name mapping.
    """

    __slots__ = ["_by_name", "_by_uid"]

    def __init__(self, passwd_fpath: StrOrPath) -> None:
        self._by_name: dict[str, int] = {}
        try:
            with open(passwd_fpath, "r") as f:
                for line in f:
                    _raw_list = line.strip().split(_SPLITTER)
                    _name, _uid = _raw_list[0], int(_raw_list[2])
                    self._by_name[_name] = _uid
            self._by_uid = {v: k for k, v in self._by_name.items()}
        except Exception as e:
            raise ValueError(f"invalid or missing {passwd_fpath=}: {e!r}")


class ParsedGroup:
    """Parse group and store name/gid mapping.

    Example group entry line:
    nogroup:x:65534:

    Attrs:
        _by_name (dict[str, int]): name:gid mapping.
        _by_gid (dict[int, str]): gid:name mapping.
    """

    __slots__ = ["_by_name", "_by_gid"]

    def __init__(self, group_fpath: StrOrPath) -> None:
        self._by_name: dict[str, int] = {}
        try:
            with open(group_fpath, "r") as f:
                for line in f:
                    _raw_list = line.strip().split(_SPLITTER)
                    self._by_name[_raw_list[0]] = int(_raw_list[2])
            self._by_gid = {v: k for k, v in self._by_name.items()}
        except Exception as e:
            raise ValueError(f"invalid or missing {group_fpath=}: {e!r}")


def map_uid_by_pwnam(*, src_db: ParsedPasswd, dst_db: ParsedPasswd, uid: int) -> int:
    """Perform src_uid -> src_name -> dst_name -> dst_uid mapping.

    Raises:
        ValueError on failed mapping.
    """
    try:
        return dst_db._by_name[src_db._by_uid[uid]]
    except KeyError:
        raise ValueError(f"failed to find mapping for {uid}")


def map_gid_by_grpnam(*, src_db: ParsedGroup, dst_db: ParsedGroup, gid: int) -> int:
    """Perform src_gid -> src_name -> dst_name -> dst_gid mapping.

    Raises:
        ValueError on failed mapping.
    """
    try:
        return dst_db._by_name[src_db._by_gid[gid]]
    except KeyError:
        raise ValueError(f"failed to find mapping for {gid}")
