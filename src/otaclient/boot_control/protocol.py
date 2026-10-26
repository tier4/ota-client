# Copyright 2022 TIER IV, INC. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from __future__ import annotations

from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable

from otaclient._types import FailureType, OTAStatus, VersionDetail

from ._ota_status_control import OTAStatusFilesControl

if TYPE_CHECKING:
    from ._ota_config_files import OTAConfigFiles


class BootControllerProtocol(Protocol):
    """Boot controller protocol for otaclient."""

    _ota_status_control: OTAStatusFilesControl

    @abstractmethod
    def get_booted_ota_status(self) -> OTAStatus:
        """Get the ota_status loaded from status file during otaclient starts up.

        This value is meant to be used only once during otaclient starts up,
            to init the live_ota_status maintained by otaclient.
        """

    @abstractmethod
    def get_standby_slot_path(self) -> Path:
        """Get the Path points to the standby slot mount point."""

    @property
    @abstractmethod
    def bootloader_type(self) -> str:
        """The type of bootloader."""

    @property
    @abstractmethod
    def standby_slot_dev(self) -> Path:
        """The device of the standby slot."""

    @abstractmethod
    def get_standby_slot_dev(self) -> str:
        """Get the dev to the standby slot."""

    @abstractmethod
    def load_version(self) -> str:
        """Read the version info from the current slot."""

    @abstractmethod
    def load_version_detail(self) -> VersionDetail | None:
        """Read the version detail from the current slot."""

    @abstractmethod
    def load_standby_slot_version(self) -> str:
        """Read the version info from the standby slot."""

    @abstractmethod
    def on_operation_failure(self) -> None:
        """Cleanup by boot_control implementation when OTA failed."""

    @abstractmethod
    def on_abort(self) -> None:
        """Cleanup by boot_control implementation when OTA is aborted."""

    @abstractmethod
    def store_failure_info(
        self, *, failure_type: FailureType, failure_reason: str
    ) -> None:
        """Persist the failure of the OTA operation to the current slot."""

    @abstractmethod
    def get_booted_failure_info(self) -> tuple[FailureType, str] | None:
        """Get the persisted failure info if the booted ota_status is FAILURE."""

    #
    # ------ update ------ #
    #

    @abstractmethod
    def pre_update(self, *, standby_as_ref: bool, erase_standby: bool): ...

    @abstractmethod
    def post_update(
        self,
        update_version: str,
        *,
        version_detail: VersionDetail | None = None,
        ota_config_files: OTAConfigFiles | None = None,
    ) -> None:
        """Configure the standby slot to be booted.

        Args:
            ota_config_files: the OTA config files(ecu_info.yaml, proxy_info.yaml)
                loaded from the OTA image, to install for the next boot.
        """

    @abstractmethod
    def finalizing_update(self, *, chroot: str | None = None) -> None:
        """Normally this method only reboots the device."""


#
# ------ partition-based layout ------ #
#


class PartitionInstallError(Exception):
    """The partition installer refused the payload, failed, or could not be run."""


@runtime_checkable
class PartitionInstallerProtocol(Protocol):
    """What writes a partition-based OTA image payload on this device.

    The implementation owns everything that touches a partition: which slot is
        standby, how a block diff is applied, what makes a boot blob and a rootfs
        blob a bootable pair, when a trial boot may be committed. otaclient asks it
        two things only: what bytes the device holds, and to install what otaclient
        has staged.
    """

    @abstractmethod
    def source_digest(self, *, size: int, data_image: str | None = None) -> str:
        """The hex sha256 of the first <size> bytes of the committed slot, or of the
            file the data image <data_image> is mounted from.

        A delta names the bytes it applies to by digest, so this is what tells
            whether a delta fits this device, before it is downloaded.

        Raises:
            PartitionInstallError: when the device cannot answer.
        """

    @abstractmethod
    def install(
        self,
        *,
        package: Path,
        version: str,
        ecu_id: str,
        release_key: str,
        on_progress: Callable[[int], None] | None = None,
    ) -> None:
        """Write the payload of the unpacked OTA image at <package> onto this device
            and arm one trial boot of it.

        The installer verifies index.jwt against its own trust anchor and every
            blob it writes against the digest the index names: otaclient's checks
            while staging are not relied on. The update is concluded by the trial
            boot otaclient reboots into afterwards, so a payload that would take
            effect without a reboot is not one this protocol describes.

        Args:
            package: the unpacked OTA image otaclient staged: index.json, index.jwt
                and the blobs this device needs.
            version: the version the installer records the payload under.
            ecu_id, release_key: which payload of a multi-ECU image is this device's.
            on_progress: called with the percent of the whole write done (every
                partition and data image together), as it is made.

        Raises:
            PartitionInstallError: when the payload is refused or the write fails.
        """


@runtime_checkable
class PartitionLayoutBootControllerProtocol(Protocol):
    """What a boot controller of a partition-based layout provides beside
        BootControllerProtocol.

    A version 1 OTA image is applied on such a boot controller by the partition
        image updater, through the installer it provides, not file by file.
    """

    @property
    @abstractmethod
    def partition_installer(self) -> PartitionInstallerProtocol:
        """What writes the payload on this device."""

    @property
    @abstractmethod
    def payload_staging_dir(self) -> Path:
        """Where this device can hold a payload, gigabytes, until the installer has
        consumed it: on persistent storage, as the session directory is a tmpfs.

        The boot controller guarantees the directory is on a mounted filesystem
            whenever an update runs. otaclient owns what it puts there: it creates a
            subdirectory of its own (0700), stages every update under it, and removes
            anything else under that subdirectory at the start of an update.
        """
