import asyncio
import os
import sys
from pathlib import Path

try:
    import app
    import ota_proxy
except ModuleNotFoundError:
    # NOTE: support for legacy way to launch otaclient
    # by directly execute `sudo python3 main.py`
    # add otaclient project base folder to the sys.path
    project_dir = Path(__file__).absolute().parent.parent
    sys.path.insert(0, str(project_dir))
finally:
    del app, ota_proxy  # cleanup as we only test import here

from app import log_util
from app.common import read_from_file, write_to_file_sync
from app.configs import config as cfg
from app.ota_client_service import launch_otaclient_grpc_server


logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)

VERSION_FILE = Path(__file__).parent.parent / "version.txt"


def _check_other_otaclient():
    """Check if there is another otaclient instance running."""
    # create a lock file to prevent multiple ota-client instances start
    lock_file = Path("/var/run/ota-client.lock")
    our_pid = os.getpid()
    if pid := read_from_file(lock_file):
        # running process will have a folder under /proc
        if Path(f"/proc/{pid}").is_dir():
            msg = f"another instance of ota-client(pid: {pid}) is running, abort"
            sys.exit(msg)
    # write our pid to the lock file
    write_to_file_sync(lock_file, f"{our_pid}")


def main():
    logger.info("started")

    version_file = VERSION_FILE
    if version_file.is_file():
        version = open(version_file).read()
        logger.info(version)

    _check_other_otaclient()

    # start the otaclient grpc server
    asyncio.run(launch_otaclient_grpc_server())


if __name__ == "__main__":
    main()
