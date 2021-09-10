"""
--> OtaClientServer -> OtaClientStub -> OtaClient
                       (callback)
"""

"""
terminology:
    partition (NG: bank)
    create, update, delete (NG: generate-update-delete, make-update-delete, create-update-remove)
    enter, leave
    begin, end
    start, stop
    initialize, finalize,
    load, store (for file)
"""

""" main.py """
import ota_client_stub
import ota_client_service

if __name__ == "__main__":
    ota_satus = OtaStatus()
    ota_client = OtaClient(ota_status)
    ota_client_stub = OtaClientStub(ota_client)
    ota_client_service = OtaClientService(ota_client_stub)

""" OtaClientService """
import grpc
import otaclient_pb2
import otaclient_pb2_grpc


class OtaClientService(otaclient_pb2_grpc.OtaClientServiceServicer):
    def __init__(self, ota_client_stub):
        self._stub = ota_client_stub

    def update(self, request, context):
        result = self._stub.update(request)
        reply = otaclient_pb2.OtaUpdateReply()
        reply.result = result
        return reply

    def rollback(self, request, context):
        result = self._stub.rollback(request)
        reply = otaclient_pb2.OtaRollbackReply()
        reply.result = result
        return reply

    def status(self, request, context):
        result = self._stub.status(request)
        reply = otaclient_pb2.OtaStatusReply()
        reply.result = result
        return reply


""" OtaClientStub """
import ota_client_call


class OtaClientStub:
    def __init__(self, ota_client):
        self._ota_client = ota_client
        self._ota_client_call = OtaClientCall("50051")

    def update(self, request):
        reply = []

        # secondary ecus
        secondary_ecu_ids = self._ota_client.get_secondary_ecu_ids()
        for secondary in secondary_ecu_ids:
            entry = OtaClientStub._find_request(request, secondary)
            if entry:
                r = self._ota_client_call.update(request, entry.ip_addr)
                reply.append(r)

        # my ecu
        ecu_id = self._ota_client.get_ecu_id()  # my ecu id
        entry = OtaClientStub._find_request(request, ecu_id)
        if entry:
            r = self._ota_client.update(entry.version, entry.url, entry.signed_cookies)
            reply.append(r)

        return reply

    def rollback(self, request):
        reply = []

        # secondary ecus
        secondary_ecu_ids = self._ota_client.get_secondary_ecu_ids()
        for secondary in secondary_ecu_ids:
            entry = OtaClientStub._find_request(request, secondary)
            if entry:
                r = self._ota_client_call.rollback(request, entry.ip_addr)
                reply.append(r)

        # my ecu
        ecu_id = self._ota_client.get_ecu_id()  # my ecu id
        entry = OtaClientStub._find_request(request, ecu_id)
        if entry:
            r = self._ota_client.rollback()
            reply.append(r)
        return reply

    def status(self, request):
        # secondary ecus
        for secondary in secondary_ecu_ids:
            r = self._ota_client_call.status(request, entry.ip_addr)
            reply.append(r)

        # my ecu
        r = self._ota_client.status()
        reply.append(r)
        return reply

    @staticmethod
    def _find_request(update_request, ecu_id):
        for request in update_request:
            if request.ecu_id == ecu_id:
                return ecu_id
        return None


""" OtaClientCall """
import grpc
import otaclient_pb2
import otaclient_pb2_grpc


class OtaClientCall:
    def __init__(self, port):
        self._port = port

    def update(self, request, ip_addr, port=None):
        target_addr = f"{ip_addr}:{port if port else self._port}"
        with grpc.insecure_channel(target_addr) as channel:
            stub = otaclient_pb2_grpc.OtaClientServiceStub(channel)
            return stub.update(request)

    def rollback(self, request, ip_addr, port=None):
        target_addr = f"{ip_addr}:{port if port else self._port}"
        with grpc.insecure_channel(target_addr) as channel:
            stub = otaclient_pb2_grpc.OtaClientServiceStub(channel)
            return stub.rollback(request)

    def status(self, request, ip_addr, port=None):
        target_addr = f"{ip_addr}:{port if port else self._port}"
        with grpc.insecure_channel(target_addr) as channel:
            stub = otaclient_pb2_grpc.OtaClientServiceStub(channel)
            return stub.status(request)


""" OtaClient """


class OtaClient:
    def __init__(self):
        self._ota_status = OtaStatusControl()
        self._mount_porint = "/mnt/standby"

    def update(self, version, url, cookies):
        self._ota_status.enter_updating(version, self._mount_point)
        # process metadata.jwt
        # process directory file
        # process symlink file
        # process regular file
        self._ota_status.leave_updating()
        # -> generate custom.cfg, grub-reboot and reboot internally

    def rollback(self):
        self._ota_status.enter_rollbacking()
        # set ota_status as `rollbacking`
        # -> check if ota_status is one of [success|rollback_failure]
        # generate custom.cfg, grub-reboot and reboot
        self._ota_status.leave_rollbacking()
        # -> generate custom.cfg, grub-reboot and reboot internally

    def status(self):
        return {
            "status": self._ota_status.get_status(),
            "failure_type": self._ota_status.get_failure_type(),
            "failure_reason": self._ota_status.get_failure_reason(),
            "firmware_version": self._ota_status._get_version(),
            "update_progress": {  # TODO
                "phase": "",
                "total_regular_files": 0,
                "regular_files_processed": 0,
            },
            "rollback_progress": {  # TODO
                "phase": "",
            },
        }


""" OtaStatus """
from enum import Enum, unique


@unique
class OtaStatus(Enum):
    INITIALIZED = 0
    SUCCESS = 1
    FAILURE = 2
    UPDATING = 3
    ROLLBACKING = 4
    ROLLBACK_FAILURE = 5


class OtaStatusControl:
    def __init__(self):
        self._ota_partition = OtaPartition()
        self._grub_control = GrubControl()
        self._ota_status = self._get_initial_ota_status()

    def enter_updating(self, mount_path):
        standby_boot = self._ota_partition.get_standby_boot_partition()
        standby_status_path = f"/boot/ota-partition.{standby_boot}/status"
        # TODO: lock if grpc is using thread
        self._store_ota_status(standby_status_path, OtaStatus.UPDATING.name)
        self._ota_status = OtaStatus.UPDATING
        # TODO: mount standby partition
        # TODO: cleanup mounted partition

    def leave_updating(self, mounted_path):
        # TODO: umount mounted_path
        self._ota_partition.switch_boot_partition()
        self._grub_control.create_custom_cfg_and_reboot()

    def enter_rollbacking(self):
        standby_boot = self._ota_partition.get_standby_boot_partition()
        standby_status_path = f"/boot/ota-partition.{standby_boot}/status"
        # TODO: lock if grpc is using thread
        self._store_ota_status(standby_status_path, OtaStatus.ROLLBACKING.name)
        self._ota_status = OtaStatus.ROLLBACKING

    def leave_rollbacking(self):
        self._ota_partition.switch_boot_partition()
        self._grub_control.create_custom_cfg_and_reboot()

    def _get_initial_ota_status(self):
        active_boot = self._ota_partition.get_active_boot_partition()
        standby_boot = self._ota_partition.get_standby_boot_partition()
        active_root = self._ota_partition.get_active_root_partition()
        standby_root = self._ota_partition.get_standby_root_partition()

        if active_boot != active_root:
            self._ota_partition.switch_boot_partition()
            self._grub_control.reboot()

        active_status_path = f"/boot/ota-partition.{active_boot}/status"
        standby_status_path = f"/boot/ota-partition.{standby_boot}/status"

        active_status = self._get_ota_status_from_file(active_status_path)
        standby_status = self._get_ota_status_from_file(standby_status_path)
        if (
            active_status == OtaStatus.INIITALIZD
            and standby_status == OtaStatus.INIITALIZD
        ):
            return OtaStatus.INITIALIZED
        if standby_status == OtaStatus.UPDATING:
            # standby status is updating w/o switching partition and (re)booted.
            return OtaStatus.FAILURE
        if standby_status == OtaStatus.ROLLBACKING:
            # standby status is rollbacking w/o switching partition and (re)booted.
            return OtaStatus.ROLLBACK_FAILURE
        if active_status == OtaStatus.UPDATING:
            self._grub_control.update_grub_cfg()
            self._store_ota_status(OtaStatus.SUCCESS.name)
            return OtaStatus.SUCCESS
        return OtaStatus[active_status]

    def _load_ota_status(self, path):
        try:
            with open(path) as f:
                status = f.read().strip()  # if it contains whitespace
                if status in [s.name for s in OtaStatus]:
                    return OtaStatus[status]
                raise ValueError(f"{path}: status={status} is illegal")
        except FileNotFoundError as e:
            return OtaStatus.INITIALIZED

    def _store_ota_status(self, path, ota_status):
        # TODO:
        # create temp file
        # and write ota_status to it
        # and move to path
        pass


""" GrubControl """


class GrubControl:
    def __init__(self):
        pass

    def create_custom_cfg_and_reboot():
        # custom.cfg
        # grub-reboot
        # reboot
        pass

    def update_grub_cfg():
        # update /etc/default/grub w/ GRUB_DISABLE_SUBMENU
        # grub-mkconfig temporally
        # count menuentry number
        # grub-mkconfig w/ the number counted
        pass

    def reboot():
        # reboot
        pass


""" OtaPartition """


class OtaPrtition:
    def __init__():
        pass

    def get_active_boot_partition():
        pass

    def get_standby_boot_partition():
        pass

    def get_active_root_partition():
        pass

    def get_standby_root_partition():
        pass

    def switch_boot_partition():
        pass
