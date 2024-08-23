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
"""Implementation of firmware update package management specification.

This specification defines how the firmware update packages are described by metadata.
    otaclient can look at these metafiles to discover available firmware update packages.
    Also, it provides a mechanism to control when otaclient should trigger the firmware update.
    Currently this module is only for NVIDIA Jetson device with UEFI Capsule update support.

A typical firmware package folder layout, assuming the firmware dir is /opt/ota/firmware:
```
/opt/ota/firmware
├── BOOTAA64.efi
├── bl_only_payload.Cap
├── firmware_dir_layout
└── firmware_manifest.yaml
```

If firmware update is needed, a file name `firmware_update.yaml` needed to be prepared under the
    firmware package dir, then the folder layout will become:
```
/opt/ota/firmware
├── BOOTAA64.efi
├── bl_only_payload.Cap
├── firmware_dir_layout
├── firmware_manifest.yaml
└── firmware_update.yaml
```
"""


from __future__ import annotations

import logging
import re
from enum import Enum
from pathlib import Path
from typing import Any, List, Literal, Union

import yaml
from pydantic import BaseModel, BeforeValidator
from typing_extensions import Annotated

from otaclient_common.typing import StrOrPath, gen_strenum_validator

logger = logging.getLogger(__name__)


class PayloadType(str, Enum):
    UEFI_CAPSULE = "UEFI-CAPSULE"
    UEFI_BOOT_APP = "UEFI-BOOT-APP"
    BUP = "BUP"


DIGEST_PATTERN = re.compile(r"^(?P<algorithm>[\w\-+ ]+):(?P<digest>[a-zA-Z0-9]+)$")


class DigestValue(BaseModel):
    """Implementation of digest value schema <algorithm>:<hexstr>."""

    algorithm: str
    digest: str

    @classmethod
    def parse(cls, _in: str | DigestValue | Any) -> DigestValue:
        if isinstance(_in, DigestValue):
            return _in

        if isinstance(_in, str):
            _in = _in.strip()
            if not (ma := DIGEST_PATTERN.match(_in)):
                raise ValueError(f"invalid digest value: {_in}")
            return DigestValue(
                algorithm=ma.group("algorithm"), digest=ma.group("digest")
            )
        raise TypeError(f"expect instance of str or {cls}, get {type(_in)}")


class NVIDIAFirmwareCompat(BaseModel):
    board_id: str
    board_sku: str
    board_rev: str
    fab_id: str
    board_conf: str

    def check_compat(self, _tnspec: str) -> bool:
        return all(
            _tnspec.find(getattr(self, field_name)) >= 0
            for field_name in self.model_fields
        )


class NVIDIAFirmwareSpec(BaseModel):
    # NOTE: let the boot control module parse BSP version
    bsp_version: str
    firmware_compat: NVIDIAFirmwareCompat


class PayloadFileLocation(BaseModel):
    """Specifying the payload file location.

    It supports file URL or digest value.

    If the location is specified by file URL, it means the payload is stored
        as a regular file on the system image.
    If the location is specified by a string with schame "<algorithm>:<hexstr>",
        it means the file is stored in the blob storage of OTA image.
    """

    location_type: Literal["blob", "file"]
    location_path: Union[str, DigestValue]

    @classmethod
    def parse(cls, _in: str | PayloadFileLocation | Any) -> PayloadFileLocation:
        if isinstance(_in, PayloadFileLocation):
            return _in

        if isinstance(_in, str):
            if _in.startswith("file://"):
                location_type = "file"
                location_path = _in.replace("file://", "", 1)
            else:
                location_type = "blob"
                location_path = DigestValue.parse(_in)
            return cls(location_type=location_type, location_path=location_path)
        raise TypeError(f"expect instance of str of {cls}, get {type(_in)}")


class FirmwarePackage(BaseModel):
    """Metadata of a firmware update package payload."""

    payload_name: str
    file_location: Annotated[
        PayloadFileLocation, BeforeValidator(PayloadFileLocation.parse)
    ]
    type: Annotated[PayloadType, BeforeValidator(gen_strenum_validator(PayloadType))]
    digest: Annotated[DigestValue, BeforeValidator(DigestValue.parse)]


class HardwareType(str, Enum):
    """
    Currently we only support NVIDIA Jetson device.
    """

    nvidia_jetson = "nvidia_jetson"


class FirmwareManifest(BaseModel):
    """Implementation of firmware manifest yaml file.

    It contains the basic information of the target hardware, and the metadata of
        all available firmware update packages.
    """

    format_version: Literal[1] = 1
    hardware_type: Annotated[
        HardwareType, BeforeValidator(gen_strenum_validator(HardwareType))
    ]
    hardware_series: str
    hardware_model: str
    firmware_spec: NVIDIAFirmwareSpec
    firmware_packages: List[FirmwarePackage]

    def check_compat(self, _tnspec: str) -> bool:
        """Check if an input compat string is compatible with this compat spec.

        Compat string example:
            2888-400-0004-M.0-1-2-jetson-xavier-rqx580-
        """
        return self.firmware_spec.firmware_compat.check_compat(_tnspec)

    def get_firmware_packages(
        self, update_request: FirmwareUpdateRequest
    ) -> list[FirmwarePackage]:
        """Get a list of firmware update packages according to update_request."""
        return [
            _payload
            for _payload in self.firmware_packages
            if _payload.payload_name in update_request.firmware_list
        ]


class FirmwareUpdateRequest(BaseModel):
    """Implementation of firmware update request.

    It contains a list of firmware to be used to update the target device.
    """

    format_version: Literal[1] = 1
    firmware_list: List[str]


def load_firmware_package(
    *,
    firmware_update_request_fpath: StrOrPath,
    firmware_manifest_fpath: StrOrPath,
) -> tuple[FirmwareUpdateRequest, FirmwareManifest] | None:
    """Parse firmware update package."""
    try:
        firmware_update_request = FirmwareUpdateRequest.model_validate(
            yaml.safe_load(Path(firmware_update_request_fpath).read_text())
        )
    except FileNotFoundError:
        logger.info("firmware update request file not found, skip firmware update")
        return
    except Exception as e:
        logger.warning(f"invalid request file: {e!r}")
        return

    try:
        firmware_manifest = FirmwareManifest.model_validate(
            yaml.safe_load(Path(firmware_manifest_fpath).read_text())
        )
    except FileNotFoundError:
        logger.warning("no firmware manifest file presented, skip firmware update!")
        return
    except Exception as e:
        logger.warning(f"invalid manifest file: {e!r}")
        return

    return firmware_update_request, firmware_manifest
