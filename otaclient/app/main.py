import asyncio
import os
import sys
from pathlib import Path

from otaclient import __version__  # type: ignore
from . import log_util
from .common import read_str_from_file, write_str_to_file_sync
from .configs import config as cfg
from .ota_client_service import launch_otaclient_grpc_server

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)

VERSION_FILE = Path(__file__).parent.parent / "version.txt"


def _check_other_otaclient():
    """Check if there is another otaclient instance running."""
    # create a lock file to prevent multiple ota-client instances start
    lock_file = Path("/var/run/ota-client.lock")
    our_pid = os.getpid()
    if pid := read_str_from_file(lock_file):
        # running process will have a folder under /proc
        if Path(f"/proc/{pid}").is_dir():
            msg = f"another instance of ota-client(pid: {pid}) is running, abort"
            sys.exit(msg)
    # write our pid to the lock file
    write_str_to_file_sync(lock_file, f"{our_pid}")


def main():
    logger.info("started")

    version_file = VERSION_FILE
    if version_file.is_file():
        version = open(version_file).read()
        logger.info(version)
    logger.info(f"otaclient version: {__version__}")
    _check_other_otaclient()

    # start the otaclient grpc server
    asyncio.run(launch_otaclient_grpc_server())


if __name__ == "__main__":
    main()
