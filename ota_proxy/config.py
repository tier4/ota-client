from dataclasses import dataclass


@dataclass
class Config:
    BASE_DIR: str = "/ota-cache"
    CHUNK_SIZE: int = 2097_152  # in bytes
    DISK_USE_LIMIT_P = 70  # in p%
    DISK_USE_PULL_INTERVAL = 2  # in seconds
    BUCKET_FILE_SIZE_LIST = (
        0,
        10_240,  # 10KiB
        102_400,  # 100KiB
        512_000,  # 500KiB
        1_048_576,  # 1MiB
        5_242_880,  # 5MiB
        10_485_760,  # 10MiB
        104_857_600,  # 100MiB
        1_048_576_000,  # 1GiB
    )  # Bytes
    DB_FILE = f"{BASE_DIR}/cache_db"

config = Config()
