"""Packed compiled protobuf files for otaclient."""
from pathlib import Path
from typing import Union


def _import_from_file(path: Union[Path, str]):
    import importlib.util
    import sys

    try:
        module_name = path.stem
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception:
        raise ImportError(f"failed to import module {module_name=} from {path=}.")


def _import_proto():
    """Import the protobuf modules to path under this folder.

    NOTE: compiled protobuf files under proto folder will be
    imported as modules to the global namespace.
    """
    import sys

    proto_dir = Path(__file__).parent
    # load modules
    # NOTE: order matters here! v2_pb2_grpc depends on v2_pb2
    files_to_load = ["otaclient_v2_pb2.py", "otaclient_v2_pb2_grpc.py"]
    for fname in files_to_load:
        _import_from_file(proto_dir / fname)


_import_proto()
del _import_proto, _import_from_file

import otaclient_v2_pb2 as v2
import otaclient_v2_pb2_grpc as v2_grpc

__all__ = ["v2", "v2_grpc"]
