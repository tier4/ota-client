class BaseOTACacheError(Exception): ...


class CacheMultiStreamingFailed(BaseOTACacheError): ...


class CacheStreamingFailed(BaseOTACacheError): ...


class StorageReachHardLimit(BaseOTACacheError): ...


class CacheStreamingInterrupt(BaseOTACacheError): ...


class CacheCommitFailed(BaseOTACacheError): ...


class ReaderPoolBusy(Exception):
    """Raised when read worker thread pool is busy."""


class CacheProviderNotReady(Exception):
    """Raised when subscriber timeout waiting cache provider."""
