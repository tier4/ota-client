class BaseConfig:
    LOGGING_FORMAT = "[%(asctime)s][%(levelname)s]: %(message)s"
    OTA_METAFILES_LIST = [
        "certificate.pem",
        "metadata.jwt",
        "dirs.txt",
        "regulars.txt",
        "symlinks.txt",
        "persistents.txt",
    ]
    OTA_METAFILE_REGULAR = "regulars.txt"
    OTA_IMAGE_DATA_DIR = "data"
    OTA_IMAGE_DATA_ZST_DIR = "data.zst"
    OTA_IMAGE_COMPRESSION_ALG = "zst"

    OUTPUT_WORKDIR = "output"
    OUTPUT_DATA_DIR = "data"
    OUTPUT_META_DIR = "meta"
    IMAGE_UNARCHIVE_WORKDIR = "images"
    MANIFEST_JSON = "manifest.json"


cfg = BaseConfig()
