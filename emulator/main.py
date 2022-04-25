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


def main(config):
    logger.info("started")

    server = None

    try:
        ecu_config = yaml.safe_load(config.read_text())["ecus"]
    except Exception as e:
        logger.warning(e)
        logger.warning(f"{config} couldn't be parsed. Default config is used instead.")
        ecu_config = DEFAULT_ECUS
    ecus = []
    logger.info(ecu_config)
    for ecu in ecu_config:
        e = Ecu(
            is_main=ecu.get("main", False),
            name=ecu.get("name", "autoware"),
            status=ecu.get("status", "INITIALIZED"),
            version=str(ecu.get("version", "")),
        )
        ecus.append(e)
    logger.info(ecus)

    def terminate(export_ecus):
        nonlocal ecus
        ecus = []
        logger.info(f"{server=}")
        service_stop(server)
        for ecu in export_ecus:
            version = (
                ecu._version_to_update
                if ecu._version_to_update is not None
                else ecu._version
            )
            status = ecu._status.status
            status = (
                "SUCCESS"
                if status == v2.StatusOta.Value("UPDATING")
                else v2.StatusOta.Name(status)
            )
            e = ecu.reset(status, version)
            ecus.append(e)

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
        logger.info("restarting. wait 10s.")
        time.sleep(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="config.yml", default="config.yml")
    args = parser.parse_args()

    logger.info(args)

    main(Path(args.config))
