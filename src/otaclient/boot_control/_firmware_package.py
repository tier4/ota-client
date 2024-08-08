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
"""


from __future__ import annotations

import re
from enum import Enum
from typing import List, Literal

from pydantic import BaseModel, BeforeValidator
from typing_extensions import Annotated

from otaclient_common.typing import gen_strenum_validator


class PayloadType(str, Enum):
    UEFI_CAPSULE = "UEFI-CAPSULE"
    UEFI_BOOT_APP = "UEFI-BOOT-APP"


DIGEST_PATTERN = re.compile(r"^(?P<algorithm>[\w\-+ ]+):(?P<digest>[a-zA-Z0-9]+)$")


class DigestValue(str):
    """Implementation of digest value schema <algorithm>:<hexstr>."""

    def __init__(self, _in: str) -> None:
        _in = _in.strip()
        if not (ma := DIGEST_PATTERN.match(_in)):
            raise ValueError(f"invalid digest value: {_in}")
        self.algorithm = ma.group("algorithm")
        self.digest = ma.group("digest")


class NVIDIAFirmwareCompat(BaseModel):
    board_id: str
    board_sku: str
    board_rev: str
    fab_id: str
    board_conf: str

    def check_compat(self, _tnspec: str) -> bool:
        for field_name in self.model_fields:
            if _tnspec.find(getattr(self, field_name)) < 0:
                return False
        return True


class NVIDIAUEFIFirmwareSpec(BaseModel):
    # NOTE: let the jetson-uefi module parse the bsp_version
    bsp_version: str
    firmware_compat: NVIDIAFirmwareCompat


class PayloadFileLocation(str):
    """Specifying the payload file location.

    It supports file URL or digest value.

    If the location is specified by file URL, it means the payload is stored
        as a regular file on the system image.
    If the location is specified by a string with schame "<algorithm>:<hexstr>",
        it means the file is stored in the blob storage of OTA image.
    """

    location_type: Literal["blob", "file"]
    location_path: str | DigestValue

    def __init__(self, _in: str) -> None:
        if _in.startswith("file://"):
            self.location_type = "file"
            self.location_path = _in.replace("file://", "", 1)
        else:
            self.location_type = "blob"
            self.location_path = DigestValue(_in)


class FirmwarePackage(BaseModel):
    """Metadata of a firmware update package payload."""

    payload_name: str
    file_location: Annotated[PayloadFileLocation, BeforeValidator(PayloadFileLocation)]
    type: Annotated[PayloadType, BeforeValidator(gen_strenum_validator(PayloadType))]
    digest: DigestValue


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
    firmware_spec: NVIDIAUEFIFirmwareSpec
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
        res = []
        for _payload in self.firmware_packages:
            if _payload.payload_name in update_request.firmware_list:
                res.append(_payload)
        return res


class FirmwareUpdateRequest(BaseModel):
    """Implementation of firmware update request.

    It contains a list of firmware to be used to update the target device.
    """

    format_version: Literal[1] = 1
    firmware_list: List[str]
