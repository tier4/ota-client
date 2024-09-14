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
"""ECU metadatas definition and parsing logic."""


from __future__ import annotations

import logging
import warnings
from enum import Enum
from pathlib import Path
from typing import List

import yaml
from pydantic import AfterValidator, BeforeValidator, Field, IPvAnyAddress
from typing_extensions import Annotated

from otaclient.configs._common import BaseFixedConfig
from otaclient_common.typing import NetworkPort, StrOrPath, gen_strenum_validator

logger = logging.getLogger(__name__)


class BootloaderType(str, Enum):
    """Bootloaders that supported by otaclient.

    auto_detect: (DEPRECATED) at runtime detecting what bootloader is in use in this ECU.
    grub: generic x86_64 platform with grub.
    cboot: ADLink rqx-580, rqx-58g, with BSP 32.5.x.
        (theoretically other Nvidia jetson xavier devices using cboot are also supported)
    rpi_boot: raspberry pi 4 with eeprom version newer than 2020-10-28(with tryboot support).
    """

    AUTO_DETECT = "auto_detect"
    GRUB = "grub"
    CBOOT = "cboot"  # deprecated, use jetson-cboot instead
    JETSON_CBOOT = "jetson-cboot"
    JETSON_UEFI = "jetson-uefi"
    RPI_BOOT = "rpi_boot"

    @staticmethod
    def deprecation_validator(value: BootloaderType) -> BootloaderType:
        if value == BootloaderType.AUTO_DETECT:
            _warning_msg = (
                "bootloader field in ecu_info.yaml is not set or set to auto_detect(DEPRECATED), "
                "runtime bootloader type detection is UNRELIABLE and bootloader field SHOULD be "
                "set in ecu_info.yaml"
            )
            warnings.warn(_warning_msg, DeprecationWarning, stacklevel=2)
            logger.warning(_warning_msg)
        return value


class ECUContact(BaseFixedConfig):
    ecu_id: str
    ip_addr: IPvAnyAddress
    # NOTE(20240327): set the default as literal for now, in the future
    #   this will be service_cfg.CLIENT_CALL_PORT.
    port: NetworkPort = 50051


class ECUInfo(BaseFixedConfig):
    """ECU info configuration.

    Attributes:
        format_version: the ecu_info.yaml scheme version, current is 1.
        ecu_id: the unique ID of this ECU.
        ip_addr: the IP address OTA servicer listening on, default is <service_config.DEFAULT_SERVER_ADDRESS>.
        bootloader: the bootloader type of this ECU.
        available_ecu_ids: a list of ECU IDs that should be involved in OTA campaign.
        secondaries: a list of ECUContact objects for sub ECUs.
    """

    format_version: int = 1
    ecu_id: str
    # NOTE(20240327): set the default as literal for now, in the future
    #   when app_configs are fully backported this will be replaced by
    #   service_cfg.DEFAULT_SERVER_ADDRESS
    ip_addr: IPvAnyAddress = Field(default="127.0.0.1")
    bootloader: Annotated[
        BootloaderType,
        BeforeValidator(gen_strenum_validator(BootloaderType)),
        AfterValidator(BootloaderType.deprecation_validator),
        Field(validate_default=False),
    ] = BootloaderType.AUTO_DETECT
    available_ecu_ids: List[str] = Field(default_factory=list)
    secondaries: List[ECUContact] = Field(default_factory=list)

    def get_available_ecu_ids(self) -> list[str]:
        """
        NOTE: this method should be used instead of directly accessing the
              available_ecu_ids attrs for backward compatibility reason.
        """
        # onetime fix, if no availabe_ecu_id is specified,
        # add my_ecu_id into the list
        if len(self.available_ecu_ids) == 0:
            return [self.ecu_id]
        return self.available_ecu_ids.copy()

    def check_ecu_ids(self, *ecu_ids: str) -> bool:
        """Check whether all the input ecu_ids are valid or not."""
        _input_ids = set(ecu_ids)
        _input_ids.discard(self.ecu_id)

        for sub_ecu in self.secondaries:
            _input_ids.discard(sub_ecu.ecu_id)

        # all the ids in input ecu_ids should be matched(so discarded)
        _invalid_ids_found = bool(_input_ids)
        if _invalid_ids_found:
            logger.warning(f"invalid ids found: {_input_ids}")
        return not _invalid_ids_found


# NOTE: this is backward compatibility for old x1 that doesn't have
#       ecu_info.yaml installed.
DEFAULT_ECU_INFO = ECUInfo(
    format_version=1,  # current version is 1
    ecu_id="autoware",  # should be unique for each ECU in vehicle
)


def parse_ecu_info(ecu_info_file: StrOrPath) -> ECUInfo:
    try:
        _raw_yaml_str = Path(ecu_info_file).read_text()
    except FileNotFoundError as e:
        logger.warning(f"{ecu_info_file=} not found: {e!r}")
        logger.warning(f"use default ecu_info: {DEFAULT_ECU_INFO}")
        return DEFAULT_ECU_INFO

    try:
        loaded_ecu_info = yaml.safe_load(_raw_yaml_str)
        assert isinstance(loaded_ecu_info, dict), "not a valid yaml file"
        return ECUInfo.model_validate(loaded_ecu_info, strict=True)
    except Exception as e:
        logger.warning(f"{ecu_info_file=} is invalid: {e!r}\n{_raw_yaml_str=}")
        logger.warning(f"use default ecu_info: {DEFAULT_ECU_INFO}")
        return DEFAULT_ECU_INFO


SELECT_ECU_CHARS_TO_CLEAN = ["\n\r"]
SELECT_ECU_COMPATIBLE_SEP = ["、，"]
SELECT_ECU_SEP_CHAR = ","


def parse_select_ecu(select_ecu_file: StrOrPath, ecu_info: ECUInfo) -> set[str]:
    """Get a list of ECU ids that should receive OTA update from select_ecu config.

    This file MUST contains a list of comma(,) separated ECU ids.

    If select_ecu config is file presented and valid, otaclient will read
        this file to load the configuration of allowing which ECUs to OTA.
        This file has higher priority over the `available_ecu_ids` field
        in the ecu_info.yaml.

    If select_ecu config is not available, use the `available_ecu_ids` field
        from the ecu_info.yaml file.
    """
    select_ecu_file = Path(select_ecu_file)
    if not select_ecu_file.is_file():
        return set(ecu_info.get_available_ecu_ids())

    raw_select_ecu_cfg = select_ecu_file.read_text()
    for _c in SELECT_ECU_CHARS_TO_CLEAN:
        raw_select_ecu_cfg = raw_select_ecu_cfg.replace(_c, " ")
    for _c in SELECT_ECU_COMPATIBLE_SEP:
        raw_select_ecu_cfg = raw_select_ecu_cfg.replace(_c, SELECT_ECU_SEP_CHAR)
    select_ecu_from_cfg = [
        _id.strip() for _id in raw_select_ecu_cfg.split(SELECT_ECU_SEP_CHAR)
    ]

    if not select_ecu_from_cfg:
        logger.warning(
            "WARN: no ECU is specified in select_ecu config file! ARE YOU SURE?"
        )
        return set()

    if ecu_info.check_ecu_ids(*select_ecu_from_cfg):
        logger.warning(
            f"WARN: only allow OTA from the following ECUs: {select_ecu_from_cfg}, "
            "not all ECUs defined in ecu_info.yaml will receive OTA!"
        )
        return set(select_ecu_from_cfg)

    logger.warning(
        "WARN: select_ecu file contains invalid ECU ids!"
        "still use `available_ecu_ids` from ecu_info.yaml"
    )
    return set(ecu_info.get_available_ecu_ids())


# NOTE(20240327): set the default as literal for now,
#   in the future this will be app_cfg.ECU_INFO_FPATH
ecu_info = parse_ecu_info(ecu_info_file="/boot/ota/ecu_info.yaml")
select_ecu_set = parse_select_ecu(
    select_ecu_file="/boot/ota/select_ecu", ecu_info=ecu_info
)
