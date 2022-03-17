import enum
from logging import INFO
from dataclasses import dataclass, field


class OTAFileCacheControl(enum.Enum):
    """Custom header for ota file caching control policies.

    format:
        Ota-File-Cache-Control: <d1>[, <d2>[, ...]]
    directives:
        retry_cache: indicates that ota_proxy should clear cache entry for <URL>
            and retry caching
        no_cache: indicates that ota_proxy should not use cache for <URL>
        use_cache: implicitly applied default value, conflicts with no_cache directive
    """

    use_cache = "use_cache"
    no_cache = "no_cache"
    retry_caching = "retry_caching"

    header = "Ota-File-Cache-Control"
    header_lower = "ota-file-cache-control"

    @classmethod
    def parse_to_value_set(cls, input: str) -> "set[str]":
        return set(input.split(","))

    @classmethod
    def parse_to_enum_set(cls, input: str) -> "set[OTAFileCacheControl]":
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
    col_type: type
    col_def: str


@dataclass
class Config:
    BASE_DIR: str = "/ota-cache"
    CHUNK_SIZE: int = 4 * 1024 * 1024  # 4MB
    REMOTE_CHUNK_SIZE: int = 1 * 1024 * 1024  # 1MB
    DISK_USE_LIMIT_SOTF_P = 60  # in p%
    DISK_USE_LIMIT_HARD_P = 70  # in p%
    DISK_USE_PULL_INTERVAL = 2  # in seconds
    BUCKET_FILE_SIZE_LIST = (
        0,
        10 * 1024,  # 10KiB
        100 * 1024,  # 100KiB
        500 * 1024,  # 500KiB
        1 * 1024 * 1024,  # 1MiB
        5 * 1024 * 1024,  # 5MiB
        10 * 1024 * 1024,  # 10MiB
        100 * 1024 * 1024,  # 100MiB
        1 * 1024 * 1024 * 1024,  # 1GiB
    )  # Bytes
    DB_FILE = f"{BASE_DIR}/cache_db"

    LOG_LEVEL = INFO

    # db config
    TABLE_NAME: str = "ota_cache"
    COLUMNS: dict = field(
        default_factory=lambda: {
            "url": ColField(str, "text UNIQUE PRIMARY KEY"),
            "hash": ColField(str, "text NOT NULL"),
            "size": ColField(int, "real NOT NULL"),
            "content_type": ColField(str, "text"),
            "content_encoding": ColField(str, "text"),
        }
    )


config = Config()
