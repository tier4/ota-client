from concurrent import futures

import grpc
import asyncio
import otaclient_v2_pb2 as v2
import otaclient_v2_pb2_grpc as v2_grpc
import configs as cfg
import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OtaClientServiceV2(v2_grpc.OtaClientServiceServicer):
    def __init__(self, ota_client_stub):
        self._stub = ota_client_stub
        self._server = None

    def Update(self, request, context):
        logger.info(f"{request=}")
        # TODO: conver grpc request to dict
        results = asyncio.run(self._stub.update(request))
        logger.info(f"{results=}")
        response = v2.UpdateResponse()
        logger.info(f"{response=}")
        for result in results:
            res_ecu = response.ecu.add()
            res_ecu.ecu_id = result["ecu_id"]
            res_ecu.result = result["result"]
        return response

    def Rollback(self, request, context):
        logger.info(f"{request=}")
        # TODO: conver grpc request to dict
        results = self._stub.rollback(request)
        logger.info(f"{results=}")
        response = v2.RollbackResponse()
        logger.info(f"{response=}")
        for result in results:
            res_ecu = response.ecu.add()
            res_ecu.ecu_id = result["ecu_id"]
            res_ecu.result = result["result"]
        return response

    def Status(self, request, context):
        # TODO: conver grpc request to dict
        results = self._stub.status(request)
        response = v2.StatusResponse()

        def set_progress(in_progress, out_progress):
            ip = in_progress
            op = out_progress

            # ecu.status.progress
            op.phase = v2.StatusProgressPhase.Value(ip["phase"])
            op.total_regular_files = ip["total_regular_files"]
            op.regular_files_processed = ip["regular_files_processed"]
            #
            op.files_processed_copy = ip["files_processed_copy"]
            op.files_processed_link = ip["files_processed_link"]
            op.files_processed_download = ip["files_processed_download"]
            op.file_size_processed_copy = ip["file_size_processed_copy"]
            op.file_size_processed_link = ip["file_size_processed_link"]
            op.file_size_processed_download = ip["file_size_processed_download"]
            op.elapsed_time_copy.FromMilliseconds(ip["elapsed_time_copy"])
            op.elapsed_time_link.FromMilliseconds(ip["elapsed_time_link"])
            op.elapsed_time_download.FromMilliseconds(ip["elapsed_time_download"])
            op.errors_download = ip["errors_download"]

        for result in results:
            res_ecu = response.ecu.add()
            res_ecu.ecu_id = result["ecu_id"]
            res_ecu.result = result["result"]
            status = result.get("status")
            if status:
                # ecu.status
                es = res_ecu.status
                es.status = v2.StatusOta.Value(status["status"])
                es.failure = v2.FailureType.Value(status["failure_type"])
                es.failure_reason = status["failure_reason"]
                es.version = status["version"]
                set_progress(status["update_progress"], es.progress)

        return response


def service_start(port, service_list):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    for service in service_list:
        service["grpc"].add_OtaClientServiceServicer_to_server(
            service["instance"], server
        )
    server.add_insecure_port(port)
    server.start()
    return server


def service_wait_for_termination(server):
    server.wait_for_termination()


def service_stop(server):
    server.stop(None)
