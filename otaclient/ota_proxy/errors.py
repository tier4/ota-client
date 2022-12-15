class BaseOTACacheError(Exception):
    ...


class CacheMultiStreamingFailed(BaseOTACacheError):
    ...


class CacheStreamingFailed(BaseOTACacheError):
    ...


class StorageReachHardLimit(BaseOTACacheError):
    ...


class CacheStreamingInterrupt(BaseOTACacheError):
    ...
