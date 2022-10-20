class BaseOTACacheError(Exception):
    ...


class CacheMultiStreamingFailed(BaseOTACacheError):
    ...


class CacheStreamingFailed(BaseOTACacheError):
    ...
