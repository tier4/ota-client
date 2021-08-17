# Generated by the gRPC Python protocol compiler plugin. DO NOT EDIT!
"""Client and server classes corresponding to protobuf-defined services."""
import grpc

import otaclient_pb2 as otaclient__pb2


class OtaClientServiceStub(object):
    """The OTA Client service definition.
    """

    def __init__(self, channel):
        """Constructor.

        Args:
            channel: A grpc.Channel.
        """
        self.OtaUpdate = channel.unary_unary(
                '/OtaClient.OtaClientService/OtaUpdate',
                request_serializer=otaclient__pb2.OtaUpdateRequest.SerializeToString,
                response_deserializer=otaclient__pb2.OtaUpdateReply.FromString,
                )
        self.OtaRollback = channel.unary_unary(
                '/OtaClient.OtaClientService/OtaRollback',
                request_serializer=otaclient__pb2.OtaRollbackRequest.SerializeToString,
                response_deserializer=otaclient__pb2.OtaRollbackReply.FromString,
                )
        self.OtaReboot = channel.unary_unary(
                '/OtaClient.OtaClientService/OtaReboot',
                request_serializer=otaclient__pb2.OtaRebootRequest.SerializeToString,
                response_deserializer=otaclient__pb2.OtaRebootReply.FromString,
                )
        self.EcuStatus = channel.unary_unary(
                '/OtaClient.OtaClientService/EcuStatus',
                request_serializer=otaclient__pb2.EcuStatusRequest.SerializeToString,
                response_deserializer=otaclient__pb2.EcuStatusReply.FromString,
                )
        self.EcuVersion = channel.unary_unary(
                '/OtaClient.OtaClientService/EcuVersion',
                request_serializer=otaclient__pb2.EcuVersionRequest.SerializeToString,
                response_deserializer=otaclient__pb2.EcuVersionReply.FromString,
                )


class OtaClientServiceServicer(object):
    """The OTA Client service definition.
    """

    def OtaUpdate(self, request, context):
        """Sends a 
        """
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details('Method not implemented!')
        raise NotImplementedError('Method not implemented!')

    def OtaRollback(self, request, context):
        """Missing associated documentation comment in .proto file."""
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details('Method not implemented!')
        raise NotImplementedError('Method not implemented!')

    def OtaReboot(self, request, context):
        """Missing associated documentation comment in .proto file."""
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details('Method not implemented!')
        raise NotImplementedError('Method not implemented!')

    def EcuStatus(self, request, context):
        """Missing associated documentation comment in .proto file."""
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details('Method not implemented!')
        raise NotImplementedError('Method not implemented!')

    def EcuVersion(self, request, context):
        """Missing associated documentation comment in .proto file."""
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details('Method not implemented!')
        raise NotImplementedError('Method not implemented!')


def add_OtaClientServiceServicer_to_server(servicer, server):
    rpc_method_handlers = {
            'OtaUpdate': grpc.unary_unary_rpc_method_handler(
                    servicer.OtaUpdate,
                    request_deserializer=otaclient__pb2.OtaUpdateRequest.FromString,
                    response_serializer=otaclient__pb2.OtaUpdateReply.SerializeToString,
            ),
            'OtaRollback': grpc.unary_unary_rpc_method_handler(
                    servicer.OtaRollback,
                    request_deserializer=otaclient__pb2.OtaRollbackRequest.FromString,
                    response_serializer=otaclient__pb2.OtaRollbackReply.SerializeToString,
            ),
            'OtaReboot': grpc.unary_unary_rpc_method_handler(
                    servicer.OtaReboot,
                    request_deserializer=otaclient__pb2.OtaRebootRequest.FromString,
                    response_serializer=otaclient__pb2.OtaRebootReply.SerializeToString,
            ),
            'EcuStatus': grpc.unary_unary_rpc_method_handler(
                    servicer.EcuStatus,
                    request_deserializer=otaclient__pb2.EcuStatusRequest.FromString,
                    response_serializer=otaclient__pb2.EcuStatusReply.SerializeToString,
            ),
            'EcuVersion': grpc.unary_unary_rpc_method_handler(
                    servicer.EcuVersion,
                    request_deserializer=otaclient__pb2.EcuVersionRequest.FromString,
                    response_serializer=otaclient__pb2.EcuVersionReply.SerializeToString,
            ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
            'OtaClient.OtaClientService', rpc_method_handlers)
    server.add_generic_rpc_handlers((generic_handler,))


 # This class is part of an EXPERIMENTAL API.
class OtaClientService(object):
    """The OTA Client service definition.
    """

    @staticmethod
    def OtaUpdate(request,
            target,
            options=(),
            channel_credentials=None,
            call_credentials=None,
            insecure=False,
            compression=None,
            wait_for_ready=None,
            timeout=None,
            metadata=None):
        return grpc.experimental.unary_unary(request, target, '/OtaClient.OtaClientService/OtaUpdate',
            otaclient__pb2.OtaUpdateRequest.SerializeToString,
            otaclient__pb2.OtaUpdateReply.FromString,
            options, channel_credentials,
            insecure, call_credentials, compression, wait_for_ready, timeout, metadata)

    @staticmethod
    def OtaRollback(request,
            target,
            options=(),
            channel_credentials=None,
            call_credentials=None,
            insecure=False,
            compression=None,
            wait_for_ready=None,
            timeout=None,
            metadata=None):
        return grpc.experimental.unary_unary(request, target, '/OtaClient.OtaClientService/OtaRollback',
            otaclient__pb2.OtaRollbackRequest.SerializeToString,
            otaclient__pb2.OtaRollbackReply.FromString,
            options, channel_credentials,
            insecure, call_credentials, compression, wait_for_ready, timeout, metadata)

    @staticmethod
    def OtaReboot(request,
            target,
            options=(),
            channel_credentials=None,
            call_credentials=None,
            insecure=False,
            compression=None,
            wait_for_ready=None,
            timeout=None,
            metadata=None):
        return grpc.experimental.unary_unary(request, target, '/OtaClient.OtaClientService/OtaReboot',
            otaclient__pb2.OtaRebootRequest.SerializeToString,
            otaclient__pb2.OtaRebootReply.FromString,
            options, channel_credentials,
            insecure, call_credentials, compression, wait_for_ready, timeout, metadata)

    @staticmethod
    def EcuStatus(request,
            target,
            options=(),
            channel_credentials=None,
            call_credentials=None,
            insecure=False,
            compression=None,
            wait_for_ready=None,
            timeout=None,
            metadata=None):
        return grpc.experimental.unary_unary(request, target, '/OtaClient.OtaClientService/EcuStatus',
            otaclient__pb2.EcuStatusRequest.SerializeToString,
            otaclient__pb2.EcuStatusReply.FromString,
            options, channel_credentials,
            insecure, call_credentials, compression, wait_for_ready, timeout, metadata)

    @staticmethod
    def EcuVersion(request,
            target,
            options=(),
            channel_credentials=None,
            call_credentials=None,
            insecure=False,
            compression=None,
            wait_for_ready=None,
            timeout=None,
            metadata=None):
        return grpc.experimental.unary_unary(request, target, '/OtaClient.OtaClientService/EcuVersion',
            otaclient__pb2.EcuVersionRequest.SerializeToString,
            otaclient__pb2.EcuVersionReply.FromString,
            options, channel_credentials,
            insecure, call_credentials, compression, wait_for_ready, timeout, metadata)
