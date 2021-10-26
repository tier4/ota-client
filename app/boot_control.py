from abc import ABC, abstractmethod
from typing import Union
from pathlib import Path

class OtaStatusControlInterface(ABC):

    @abstractmethod
    def store_env(self, key: str, value):
        pass

    @abstractmethod
    def load_ota_version(self):
        pass

    # @abstractmethod
    # def get_standby_boot_partition(self, type="path") -> Union[Path, str]:
    #     pass

    # @abstractmethod
    # def get_active_boot_partition(self, type="path") -> Union[Path, str]:
    #     pass

class BootControlInterface(
    OtaStatusControlInterface, 
    ABC):

    @abstractmethod
    def reboot_switch_boot(self):
        pass

    @abstractmethod
    def reboot(self):
        pass

    @abstractmethod
    def pre_update(self, mount_point):
        pass

    @abstractmethod
    def post_update(self, mount_point):
        """
        post_update finalizes the update process
        """
        pass

    @abstractmethod
    def pre_rollback(self):
        pass

    @abstractmethod
    def post_rollback(self):
        pass
