"""
terminology:
    partition (NG: bank)
    create, update, delete (NG: generate-update-delete, make-update-delete, create-update-remove)
    enter, leave
    begin, end
    start, stop
    initialize, finalize,
    load, store (for file)
    request, response (NG: reply)
    EcuInfo, EcuId (NG: Ecuinfo, ecuinfo, Ecuid, ecuid)
backup file name:
    {original}.old
"""

""" main.py """
import ota_client_stub
import ota_client_service

if __name__ == "__main__":
    ota_client_stub = OtaClientStub()
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
        response = otaclient_pb2.UpdateResponse()
        response.result = result
        return response

    def rollback(self, request, context):
        result = self._stub.rollback(request)
        response = otaclient_pb2.RollbackResponse()
        response.result = result
        return response

    def status(self, request, context):
        result = self._stub.status(request)
        response = otaclient_pb2.StatusResponse()
        response.result = result
        return response


""" OtaClientStub """
import ota_client
import ota_client_call
import ecu_info


class OtaClientStub:
    def __init__(self):
        self._ota_client = OtaClient()
        self._ecu_info = EcuInfo()
        self._ota_client_call = OtaClientCall("50051")

    def update(self, request):
        response = []

        # secondary ecus
        secondary_ecus = self._ecu_info.get_secondary_ecus()
        for secondary in secondary_ecus:
            entry = OtaClientStub._find_request(request, secondary)
            if entry:
                r = self._ota_client_call.update(request, secondary["ip_addr"])
                response.append(r)

        # my ecu
        ecu_id = self._ecu_info.get_ecu_id()  # my ecu id
        entry = OtaClientStub._find_request(request, ecu_id)
        if entry:
            r = self._ota_client.update(entry.version, entry.url, entry.signed_cookies)
            response.append(r)

        return response

    def rollback(self, request):
        response = []

        # secondary ecus
        secondary_ecus = self._ecu_info.get_secondary_ecus()
        for secondary in secondary_ecus:
            entry = OtaClientStub._find_request(request, secondary)
            if entry:
                r = self._ota_client_call.rollback(request, secondary["ip_addr"])
                response.append(r)

        # my ecu
        ecu_id = self._ecu_info.get_ecu_id()  # my ecu id
        entry = OtaClientStub._find_request(request, ecu_id)
        if entry:
            r = self._ota_client.rollback()
            response.append(r)
        return response

    def status(self, request):
        response = []

        # secondary ecus
        secondary_ecus = self._ecu_info.get_secondary_ecus()
        for secondary in secondary_ecus:
            r = self._ota_client_call.status(request, secondary["ip_addr"])
            response.append(r)

        # my ecu
        r = self._ota_client.status()
        response.append(r)
        return response

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

    def get_boot_standby_path(self):
        standby_boot = self._ota_partition.get_standby_boot_device()
        return f"/boot/ota-partition.{standby_boot}/"

    def get_ota_status(self):
        return self._ota_status

    def enter_updating(self, version, mount_path):
        if self.ota_status not in [
            OtaStatus.INITIALIZED,
            OtaStatus.SUCCESS,
            OtaStatus.FAILURE,
            OtaStatus.ROLLBACK_FAILURE,
        ]:
            raise ValueError(f"status={self.status} is illegal for update")
        standby_boot = self._ota_partition.get_standby_boot_device()
        standby_status_path = f"/boot/ota-partition.{standby_boot}/status"
        standby_version_path = f"/boot/ota-partition.{standby_boot}/version"
        self._store_ota_version(standby_version_path, version)
        self._store_ota_status(standby_status_path, OtaStatus.UPDATING.name)
        self._ota_status = OtaStatus.UPDATING
        # TODO: mount standby partition
        # TODO: cleanup mounted partition

    def leave_updating(self, mounted_path):
        # TODO: umount mounted_path
        standby_boot = self._ota_partition.get_standby_boot_device()
        self._ota_partition.update_fstab_root_partition(standby_boot)
        self._ota_partition.update_boot_partition(standby_boot)
        self._grub_control.create_custom_cfg_and_reboot()

    def enter_rollbacking(self):
        if self.ota_status not in [
            OtaStatus.SUCCESS,
            OtaStatus.ROLLBACK_FAILURE,
        ]:
            raise ValueError(f"status={self.status} is illegal for rollback")
        standby_boot = self._ota_partition.get_standby_boot_device()
        standby_status_path = f"/boot/ota-partition.{standby_boot}/status"
        self._store_ota_status(standby_status_path, OtaStatus.ROLLBACKING.name)
        self._ota_status = OtaStatus.ROLLBACKING

    def leave_rollbacking(self):
        standby_boot = self._ota_partition.get_standby_boot_device()
        self._ota_partition.update_fstab_root_partition(standby_boot)
        self._ota_partition.update_boot_partition(standby_boot)
        self._grub_control.create_custom_cfg_and_reboot()

    def _get_initial_ota_status(self):
        active_boot = self._ota_partition.get_active_boot_device()
        standby_boot = self._ota_partition.get_standby_boot_device()
        active_root = self._ota_partition.get_active_root_device()
        standby_root = self._ota_partition.get_standby_root_device()

        if active_boot != active_root:
            self._ota_partition.update_boot_partition(active_root)
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

    def _load_ota_version(self, path):
        try:
            with open(path) as f:
                version = f.read().strip()  # if it contains whitespace
                return version
        except FileNotFoundError as e:
            return ""

    def _store_ota_version(self, path, version):
        # TODO:
        # create temp file
        # and write ota_status to it
        # and move to path
        pass


""" GrubControl """


class GrubControl:
    @staticmethod
    def create_custom_cfg_and_reboot():
        # custom.cfg
        # count custom.cfg menuentry number
        # grub-reboot
        # reboot
        pass

    @staticmethod
    def update_grub_cfg():
        # update /etc/default/grub w/ GRUB_DISABLE_SUBMENU
        # grub-mkconfig temporally
        # count menuentry number
        # grub-mkconfig w/ the number counted
        pass

    @staticmethod
    def reboot():
        # reboot
        pass


""" OtaPartition """


class OtaPrtition:
    """
    NOTE:
    device means: sda3
    device_file means: /dev/sda3
    """

    def __init__(self):
        self._active_boot_device_cache = None
        self._standby_boot_device_cache = None
        self._active_root_device_cache = None
        self._standby_root_device_cache = None
        self._fstab_file = "/etc/fstab"

    def get_active_boot_device(self):
        if self._active_boot_device:  # return cache if available
            return self._active_boot_device
        # read link
        try:
            link = os.readlink("/boot/ota-partition")
        except FileNotFoundError:
            # TODO: backward compatibility
            return None
        m = re.match(r"/boot/ota-partition.(.*)", link)
        return m.group(1)

    def get_standby_boot_device(self):
        if self._standby_boot_device:  # return cache if available
            return self._standby_boot_device
        active_root_device = self.get_active_root_device()
        standby_root_device = self.get_standby_root_device()

        active_boot_device = self.get_active_boot_device()
        if active_boot_device == active_root_device:
            return standby_root_device
        if active_boot_device == standby_root_device:
            return active_root_device
        raise ValueError(
            f"illegal active_boot_device={active_boot_device}, "
            f"active_boot_device={active_root_device}, "
            f"standby_root_device={standby_root_device}"
        )

    def get_active_root_device():
        if self._active_root_device:  # return cache if available
            return self._active_root_device

        self._active_root_device = _get_root_device().lstrip("/dev")
        return self._active_root_device

    def get_standby_root_device():
        if self._standby_root_device:  # return cache if available
            return self._standby_root_device

        # find root device
        root_device_file = _get_root_device_file()

        # find boot device
        boot_device_file = _get_boot_device_file()

        # find parent device from root device
        parent_device_file = _get_parent_device_file(root_device_file)

        # find standby device file from root and boot device file
        self._standby_root_device = _get_standby_device_file(
            parent_device_file,
            root_device_file,
            boot_device_file,
        ).lstrip("/dev")
        return self._standby_root_device

    def update_boot_partition(boot_device):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            link = os.path.join(d, "templink")
            # create link file to link /boot/ota-partition.{boot_device}
            os.symlink(f"ota-partition.{boot_device}", link)
            # move link created to /boot/ota-partition
            os.rename(link, "/boot/ota-partition")

    def update_fstab_root_partition(device):
        # retrieve uuid from the partion
        # retrieve device file from the partion
        updated_fstab = []
        fstab = open(self._fstab_file).readlines()
        for line in fstab:
            if line.startswith("#")
                updated_fstab.append(line)
                continue
            line_split = line.split()
            if line_split[1]  == "/":
                # TODO
                # replace UUID=... or device file
                # if line_split[0].find("UUID="):
                #     line.replace()
                # elif line_split[0].find(device):
                #     line.replace()
                # ...
                pass
            else:
                updated_fstab.append(line)

        with tempfile.NamedTemporaryFile("w", delete=False, prefix=__name__) as f:
            temp_name = f.name
            f.writelines(updated_fstab)
            shutil.copy(self._fstab_file, f"{self._fstab_file}.old")
            shutil.move(temp_name, self._fstab_file)

    """ private from here """

    def _findmnt_cmd(mount_point):
        cmd = "findmnt -n -o SOURCE {mount_point}"
        return subprocess.check_output(shlex.split(cmd))

    def _get_root_device_file():
        return _findmnt_cmd("/").decode().strip()

    def _get_boot_device_file():
        return _findmnt_cmd("/boot").decode().strip()

    def _get_parent_device_file(child_device_file):
        cmd = f"lsblk -ipno PKNAME {device_file}"
        return subprocess.check_output(shlex.split(cmd))

    def _get_standby_device_file(
        parent_device_file, root_device_file, boot_device_file
    ):
        # list children device file with parent
        cmd = f"lsblk -Pp -o NAME,FSTYPE {parent_device_file}"
        output = subprocess.check_output(shlex.split(cmd))
        # FSTYPE="ext4" and
        # not (parent_device_file, root_device_file and boot device_file)
        for blk in output.decode().split("\n"):
            m = re.match(r'NAME="(.*)" FSTYPE="(.*)"', blk)
            if (
                m.group(1) != parent_device_file
                and m.group(1) != root_device_file
                and m.group(1) != boot_device_file
                and m.group(2) == "ext4"
            ):
                return m.group(1)
        raise ValueError(f"lsblk output={output} is illegal")


""" EcuInfo """
import yaml


class EcuInfo:
    def __init__(self):
        ecu_info_path = "/boot/ota/ecu_info.yaml"
        self._ecu_info = self._load_ecu_info(ecu_info_path)

    def get_secondary_ecus():
        return self._ecu_info["secondaries"]

    def get_ecu_id():
        return self._ecu_info["ecu_id"]

    def _load_ecu_info(self):
        with open(self._ecu_info_path) as f:
            ecu_info = yaml.load(f, Loader=yaml.SafeLoader)
            format_version = ecu_info["format_version"]
            if format_version != 1:
                raise ValueError(f"format_version={format_version} is illegal")
            return ecu_info
