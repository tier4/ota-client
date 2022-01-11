###### load path ######
def _path_load():
    import sys
    from pathlib import Path

    project_base = Path(__file__).absolute().parent.parent
    sys.path.extend([str(project_base), str(project_base / "app")])


_path_load()
######

import argparse
from typing import List
import grpc
import yaml
import time
import signal
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import app.otaclient_v2_pb2_grpc as v2_grpc
import app.otaclient_v2_pb2 as v2

import logutil
import logging

logger: logging.Logger = logutil.get_logger(__name__, logging.DEBUG)

_DEFAULT_PORT = "50051"
_MODE = {"standalone", "mainecu", "subecus"}


class MiniOtaClientServiceV2(v2_grpc.OtaClientServiceServicer):
    def __init__(self, ecu_id: dict):
        self.ecu_id = ecu_id

        self._executor = ThreadPoolExecutor()
        # critical zone
        self._lock = Lock()
        self._update_finished = True
        # end of critical zone

    def _update_counter(self):
        with self._lock:
            time.sleep(20)
            self._update_finished = True

    def Update(self, request, context: grpc.ServicerContext):
        peer = context.peer()
        logger.debug(f"receive update request from {peer=}")
        logger.debug(f"{request=}")

        results = v2.UpdateResponse()
        ecu = results.ecu.add()
        ecu.ecu_id = self.ecu_id
        ecu.result = v2.NO_FAILURE

        with self._lock:
            self._update_finished = False
            self._executor.submit(self._update_counter)

        return results

    def Rollback(self, request, context: grpc.ServicerContext):
        # NOTE: not yet used!
        peer = context.peer()
        logger.info(f"receive rollback request from {peer=}")
        logger.debug(f"{request=}")

        results = v2.RollbackResponse()
        return results

    def Status(self, request, context: grpc.ServicerContext):
        peer = context.peer()
        logger.debug(f"receive status request from {peer=}")

        result = v2.StatusResponse()
        ecu = result.ecu.add()
        ecu.ecu_id = self.ecu_id
        ecu.result = v2.NO_FAILURE

        ecu_status = ecu.status
        if self._update_finished:
            ecu_status.status = v2.SUCCESS
        else:
            ecu_status.status = v2.UPDATING

        return result


def server_start(
    pool: ThreadPoolExecutor, ecu_id: str, ip: str, port: str = "50051"
) -> grpc.server:
    service = MiniOtaClientServiceV2(ecu_id)

    server = grpc.server(pool)
    v2_grpc.add_OtaClientServiceServicer_to_server(service, server)

    server.add_insecure_port(f"{ip}:{port}")
    server.start()
    logger.info(f"start {ecu_id=} at {ip}:{port}")

    return server


def mainecu_mode(executor: ThreadPoolExecutor, ecu_info: dict) -> List[grpc.server]:
    f = executor.submit(
        server_start,
        ecu_id=ecu_info["ecu_id"],
        pool=executor,
        ip=ecu_info["ip_addr"],
        port=ecu_info.get("port", _DEFAULT_PORT),
    )
    server = f.result()
    return [
        server,
    ]


def subecu_mode(executor: ThreadPoolExecutor, ecu_info: dict) -> List[grpc.server]:
    # schedule the servers to the thread pool
    futures = []
    for subecu in ecu_info["secondaries"]:
        futures.append(
            executor.submit(
                server_start,
                ecu_id=subecu["ecu_id"],
                pool=executor,
                ip=subecu["ip_addr"],
                port=subecu.get("port", _DEFAULT_PORT),
            )
        )

    servers = [f.result() for f in futures]
    return servers


def standalone_mode(
    executor: ThreadPoolExecutor, args: argparse.Namespace
) -> List[grpc.server]:
    f = executor.submit(
        server_start,
        ecu_id="standalone",
        pool=executor,
        ip=args.ip,
        port=args.port,
    )
    server = f.result()
    return [
        server,
    ]


def load_ecu_info(ecu_info_file: str) -> dict:
    with open(ecu_info_file, "r") as f:
        return yaml.safe_load(f)


def _sign_handler_wrapper(server_list: List[grpc.server], executor: ThreadPoolExecutor):
    def _sign_handler(signum, frame):
        logger.info(f"receive signal {signum}")
        if signum == signal.SIGINT:
            logger.warning("terminating all servers...")
            for s in server_list:
                s.stop(None)
            logger.info("close the threadpool")
            executor.shutdown()
        raise InterruptedError("terminating the mini ota_client servers")

    return _sign_handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="calling main ECU's API")
    parser.add_argument("-c", "--ecu_info", default="ecu_info.yaml", help="ecu_info")
    parser.add_argument(
        "mode",
        default="standalone",
        help=(
            "running mode for mini_ota_client(standalone, subecus, mainecu)\n"
            "\tstandalone: run a single mini_ota_client\n"
            "\tmainecu: run a single mini_ota_client as mainecu according to ecu_info.yaml\n"
            "\tsubecus: run subecu(s) according to ecu_info.yaml"
        ),
    )
    parser.add_argument(
        "--ip",
        default="127.0.0.1",
        help="(standalone) listen at IP(default: localhost)",
    )
    parser.add_argument(
        "--port",
        default=_DEFAULT_PORT,
        help=f"(standalone) use port PORT(default: {_DEFAULT_PORT})",
    )

    args = parser.parse_args()
    ecu_info_file = Path(args.ecu_info)

    if args.mode not in _MODE:
        parser.error(f"invalid mode {args.mode}, should be one of {_MODE}")

    if args.mode != "standalone":
        if not ecu_info_file.is_file():
            parser.error(
                f"invalid {ecu_info_file=!r}. ecu_info.yaml is required for non-standalone mode"
            )

    ecu_info = yaml.safe_load(ecu_info_file.read_text())

    executor = ThreadPoolExecutor()
    servers_list: List[grpc.server] = []
    if args.mode == "subecus":
        logger.info("subecus mode")
        servers_list = subecu_mode(executor, ecu_info)
    elif args.mode == "mainecu":
        logger.info("mainecu mode")
        servers_list = mainecu_mode(executor, ecu_info)
    elif args.mode == "standalone":
        logger.info("standalone mode")
        servers_list = standalone_mode(executor, args)

    # cleanup the executor in the sign handler
    signal.signal(signal.SIGINT, _sign_handler_wrapper(servers_list, executor))

    while True:
        pass  # wait for the SIGINT
