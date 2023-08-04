class BaseConfig:
    LOGGING_FORMAT = (
        "[%(asctime)s][%(levelname)s]-%(name)s:%(funcName)s:%(lineno)d,%(message)s"
    )
    OTA_METAFILES_LIST = [
        "certificate.pem",
        "metadata.jwt",
        "dirs.txt",
        "regulars.txt",
        "symlinks.txt",
        "persistents.txt",
    ]
    OTA_IMAGE_DATA_DIR = "data"
    OTA_IMAGE_DATA_ZST_DIR = "data.zst"

    OTA_IMAGE_COMPRESSION_ALG = "zst"


cfg = BaseConfig()
