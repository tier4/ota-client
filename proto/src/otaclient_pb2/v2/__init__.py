try:
    from otaclient_pb2.v2._version import (
        __version__,
        version,
        __version_tuple__,
        version_tuple,
    )
except ImportError:
    version = __version__ = "0.0.0"

__all__ = ["version", "__version__", "version_tuple", "__version_tuple__"]
