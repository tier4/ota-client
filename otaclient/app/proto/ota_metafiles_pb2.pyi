from typing import ClassVar as _ClassVar
from typing import Optional as _Optional

from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message

DESCRIPTOR: _descriptor.FileDescriptor

class DirectoryInf(_message.Message):
    __slots__ = ["gid", "mode", "path", "uid"]
    GID_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    UID_FIELD_NUMBER: _ClassVar[int]
    gid: int
    mode: int
    path: str
    uid: int
    def __init__(
        self,
        mode: _Optional[int] = ...,
        uid: _Optional[int] = ...,
        gid: _Optional[int] = ...,
        path: _Optional[str] = ...,
    ) -> None: ...

class PersistentInf(_message.Message):
    __slots__ = ["path"]
    PATH_FIELD_NUMBER: _ClassVar[int]
    path: str
    def __init__(self, path: _Optional[str] = ...) -> None: ...

class RegularInf(_message.Message):
    __slots__ = [
        "compressed_alg",
        "gid",
        "inode",
        "mode",
        "nlink",
        "path",
        "sha256hash",
        "size",
        "uid",
    ]
    COMPRESSED_ALG_FIELD_NUMBER: _ClassVar[int]
    GID_FIELD_NUMBER: _ClassVar[int]
    INODE_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    NLINK_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    SHA256HASH_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    UID_FIELD_NUMBER: _ClassVar[int]
    compressed_alg: str
    gid: int
    inode: int
    mode: int
    nlink: int
    path: str
    sha256hash: bytes
    size: int
    uid: int
    def __init__(
        self,
        mode: _Optional[int] = ...,
        uid: _Optional[int] = ...,
        gid: _Optional[int] = ...,
        nlink: _Optional[int] = ...,
        sha256hash: _Optional[bytes] = ...,
        path: _Optional[str] = ...,
        size: _Optional[int] = ...,
        inode: _Optional[int] = ...,
        compressed_alg: _Optional[str] = ...,
    ) -> None: ...

class SymbolicLinkInf(_message.Message):
    __slots__ = ["gid", "mode", "slink", "srcpath", "uid"]
    GID_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    SLINK_FIELD_NUMBER: _ClassVar[int]
    SRCPATH_FIELD_NUMBER: _ClassVar[int]
    UID_FIELD_NUMBER: _ClassVar[int]
    gid: int
    mode: int
    slink: str
    srcpath: str
    uid: int
    def __init__(
        self,
        mode: _Optional[int] = ...,
        uid: _Optional[int] = ...,
        gid: _Optional[int] = ...,
        slink: _Optional[str] = ...,
        srcpath: _Optional[str] = ...,
    ) -> None: ...
