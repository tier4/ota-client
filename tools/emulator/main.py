import time
import path_loader  # noqa

from pathlib import Path
import yaml

from ota_client_stub import OtaClientStub
from ota_client_service import (
    OtaClientServiceV2,
    service_start,
    service_wait_for_termination,
    service_stop,
)
import otaclient_v2_pb2_grpc as v2_grpc
import otaclient_v2_pb2 as v2

from configs import config as cfg
from configs import server_cfg
import log_util

from ecu import Ecu

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)

DEFAULT_ECUS = [
    {"main": True, "id": "autoware", "status": "INITIALIZED", "version": "123.456"}
]


def main(config_file):
    logger.info("started")

    server = None

    try:
        config = yaml.safe_load(config_file.read_text())
        ecu_config = config["ecus"]
    except Exception as e:
        logger.warning(e)
        logger.warning(
            f"{config_file} couldn't be parsed. Default config is used instead."
        )
        ecu_config = DEFAULT_ECUS
    ecus = []
    logger.info(ecu_config)
    for ecu in ecu_config:
        e = Ecu(
            is_main=ecu.get("main", False),
            name=ecu.get("name", "autoware"),
            status=ecu.get("status", "INITIALIZED"),
            version=str(ecu.get("version", "")),
            time_to_update=ecu.get("time_to_update"),
            time_to_restart=ecu.get("time_to_restart"),
        )
        ecus.append(e)
    logger.info(ecus)

    def terminate(restart_time):
        logger.info(f"{server=}")
        service_stop(server)
        logger.info(f"restarting. wait {restart_time}s.")
        time.sleep(restart_time)

    while True:
        ota_client_stub = OtaClientStub(ecus, terminate)
        ota_client_service_v2 = OtaClientServiceV2(ota_client_stub)

        logger.info("starting grpc server.")
        server = service_start(
            f"localhost:{server_cfg.SERVER_PORT}",
            [
                {"grpc": v2_grpc, "instance": ota_client_service_v2},
            ],
        )

        service_wait_for_termination(server)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="config.yml", default="config.yml")
    args = parser.parse_args()

    logger.info(args)

    main(Path(args.config))
