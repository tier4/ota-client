from logging import INFO
from dataclasses import dataclass, field


@dataclass
class Config:
    BASE_DIR: str = "/ota-cache"
    CHUNK_SIZE: int = 4 * 1024 * 1024  # 4MB
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
            "url": (str, "text UNIQUE PRIMARY KEY"),
            "hash": (str, "text NOT NULL"),
            "size": (int, "real NOT NULL"),
            "content_type": (str, "text"),
            "content_encoding": (str, "text"),
        }
    )


config = Config()
