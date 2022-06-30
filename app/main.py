import asyncio
import sys
import os
from pathlib import Path
import _pathloader

assert _pathloader

from app import log_util
from app.configs import config as cfg
from app.ota_client_service import launch_otaclient_grpc_server

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)

VERSION_FILE = Path(__file__).parent.parent / "version.txt"


def _check_other_otaclient():
    # create a lock file to prevent multiple ota-client instances start
    lock_file = Path("/var/run/ota-client.lock")
    our_pid = os.getpid()
    if lock_file.is_file():
        pid = lock_file.read_text()
        # running process will have a folder under /proc
        if Path(f"/proc/{pid}").is_dir():
            msg = f"another instance of ota-client(pid: {pid}) is running, abort"
            sys.exit(msg)
    # write our pid to the lock file
    lock_file.write_text(f"{our_pid}")


def main():
    version_file = VERSION_FILE
    if version_file.is_file():
        version = open(version_file).read()
        logger.info(version)

    if cfg is None:
        sys.exit("unsupported platform, abort")

    _check_other_otaclient()

    # start the otaclient grpc server
    asyncio.run(launch_otaclient_grpc_server())


if __name__ == "__main__":
    main()
