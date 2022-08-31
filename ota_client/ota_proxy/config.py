import enum
from logging import INFO
from dataclasses import dataclass, field
from typing import Dict, Protocol, Tuple, Type, TypeVar, Set

_T = TypeVar("_T")


class OTAFileCacheControl(enum.Enum):
    """Custom header for ota file caching control policies.

    format:
        Ota-File-Cache-Control: <directive>
    directives:
        retry_cache: indicates that ota_proxy should clear cache entry for <URL>
            and retry caching
        no_cache: indicates that ota_proxy should not use cache for <URL>
        use_cache: implicitly applied default value, conflicts with no_cache directive
            no need(and no effect) to add this directive into the list

    NOTE: using retry_cache and no_cache together will not work as expected,
        only no_cache will be respected, already cached file will not be deleted as retry_cache indicates.
    """

    use_cache = "use_cache"
    no_cache = "no_cache"
    retry_caching = "retry_caching"

    header = "Ota-File-Cache-Control"
    header_lower = "ota-file-cache-control"

    @classmethod
    def parse_to_value_set(cls, input: str) -> Set[str]:
        return set(input.split(","))

    @classmethod
    def parse_to_enum_set(cls, input: str) -> Set["OTAFileCacheControl"]:
        _policies_set = cls.parse_to_value_set(input)
        res = set()
        for p in _policies_set:
            res.add(OTAFileCacheControl[p])

        return res

    @classmethod
    def add_to(cls, target: str, input: "OTAFileCacheControl") -> str:
        _policies_set = cls.parse_to_value_set(target)
        _policies_set.add(input.value)
        return ",".join(_policies_set)


@dataclass(frozen=True)
class ColField:
    col_type: Type
    col_def: str


@dataclass
class Config:
    BASE_DIR: str = "/ota-cache"
    CHUNK_SIZE: int = 4 * 1024 * 1024  # 4MB
    DISK_USE_LIMIT_SOFT_P = 70  # in p%
    DISK_USE_LIMIT_HARD_P = 80  # in p%
    DISK_USE_PULL_INTERVAL = 2  # in seconds
    # value is the largest numbers of files that
    # might need to be deleted for the bucket to hold a new entry
    # if we have to reserve space for this file.
    BUCKET_FILE_SIZE_DICT = {
        0: 0,  # not filtered
        2 * 1024: 1,  # 2KiB
        3 * 1024: 1,
        4 * 1024: 1,
        5 * 1024: 1,
        8 * 1024: 2,
        16 * 1024: 2,  # 16KiB
        32 * 1024: 8,
        256 * 1024: 16,  # 256KiB
        4 * (1024**2): 2,  # 4MiB
        8 * (1024**2): 32,  # 8MiB
        256 * (1024**2): 2,
        512 * (1024**2): 0,  # not filtered
    }
    DB_FILE = f"{BASE_DIR}/cache_db"

    LOG_LEVEL = INFO

    # DB configuration/setup
    # ota-cache table
    # NOTE: use table name to keep track of table scheme version
    TABLE_DEFINITION_VERSION = "v2"
    TABLE_NAME: str = f"ota_cache_{TABLE_DEFINITION_VERSION}"
    COLUMNS: Dict[str, ColField] = field(
        default_factory=lambda: {
            "url": ColField(str, "TEXT UNIQUE NOT NULL PRIMARY KEY"),
            "bucket": ColField(int, "INTEGER NOT NULL"),
            "last_access": ColField(float, "REAL NOT NULL"),
            "hash": ColField(str, "TEXT NOT NULL"),
            "size": ColField(int, "INTEGER NOT NULL"),
            "content_type": ColField(str, "TEXT"),
            "content_encoding": ColField(str, "TEXT"),
        }
    )

    BUCKET_LAST_ACCESS_IDX: str = (
        f"CREATE INDEX bucket_last_access_idx ON {TABLE_NAME}(bucket, last_access)"
    )


class CacheMetaProtocol(Protocol):
    """Definition for CacheMeta class.

    Check Config.COLUMNS for details.
    """

    url: str
    bucket: int
    last_access: float
    hash: str
    size: int
    content_type: str
    content_encoding: str

    @classmethod
    def shape(cls) -> str:
        ...

    def to_tuple(self) -> Tuple[_T]:
        ...

    @classmethod
    def row_to_meta(cls, row: Dict[str, _T]) -> "CacheMetaProtocol":
        ...


config = Config()
