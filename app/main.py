import sys
import os
from pathlib import Path
import _pathloader

assert _pathloader

from app.ota_client_stub import OtaClientStub
from app.ota_client_service import (
    OtaClientServiceV2,
    service_start,
    service_wait_for_termination,
)
import app.otaclient_v2_pb2_grpc as v2_grpc

from app import log_util
from app.configs import config as cfg
from app.configs import server_cfg

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)

VERSION_FILE = Path(__file__).parent.parent / "version.txt"


def main():
    logger.info("started")
    version_file = VERSION_FILE
    if version_file.is_file():
        version = open(version_file).read()
        logger.info(version)

    if cfg is None:
        sys.exit("unsupported platform, abort")

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

    ota_client_stub = OtaClientStub()
    ota_client_service_v2 = OtaClientServiceV2(ota_client_stub)

    server = service_start(
        f"{ota_client_stub.host_addr()}:{server_cfg.SERVER_PORT}",
        [
            {"grpc": v2_grpc, "instance": ota_client_service_v2},
        ],
    )

    service_wait_for_termination(server)


if __name__ == "__main__":
    main()
