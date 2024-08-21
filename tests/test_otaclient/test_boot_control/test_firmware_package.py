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

import pytest
import yaml

from otaclient.boot_control._firmware_package import (
    DigestValue,
    FirmwareManifest,
    FirmwarePackage,
    FirmwareUpdateRequest,
    HardwareType,
    NVIDIAFirmwareCompat,
    NVIDIAUEFIFirmwareSpec,
    PayloadFileLocation,
    PayloadType,
)


@pytest.mark.parametrize(
    "_in, _expected",
    (
        (
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            [
                "sha256",
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            ],
        ),
        (
            "sha1:da39a3ee5e6b4b0d3255bfef95601890afd80709",
            [
                "sha1",
                "da39a3ee5e6b4b0d3255bfef95601890afd80709",
            ],
        ),
    ),
)
def test_digest_value_parsing(_in, _expected: list[str]):
    _parsed = DigestValue(_in)
    assert _parsed.algorithm == _expected[0]
    assert _parsed.digest == _expected[1]


@pytest.mark.parametrize(
    "_in, _expected",
    (
        (
            "file:///opt/ota/firmware/BOOTAA64.efi",
            ["file", "/opt/ota/firmware/BOOTAA64.efi"],
        ),
        (
            _in := "sha256:32baa6f7e96661d50fb78e5d7149763e3a0fe70c51c37c6bea92c3c27cd2472d",
            [
                "blob",
                DigestValue(_in),
            ],
        ),
    ),
)
def test_payload_file_location(_in, _expected: list[str] | list[str | DigestValue]):
    _parsed = PayloadFileLocation(_in)
    assert _parsed.location_type == _expected[0]
    assert _parsed.location_path == _expected[1]


@pytest.mark.parametrize(
    "_spec, _compat_str, _expected",
    (
        # rqx58g compat against rqx58g compat str
        (
            NVIDIAFirmwareCompat(
                board_id="2888",
                board_sku="0004",
                board_rev="M.0",
                fab_id="400",
                board_conf="jetson-xavier-rqx580",
            ),
            "2888-400-0004-M.0-1-2-jetson-xavier-rqx580-",
            True,
        ),
        # rqx580 compat against rqx58g compat str
        (
            NVIDIAFirmwareCompat(
                board_id="2888",
                board_sku="0004",
                board_rev="L.0",
                fab_id="400",
                board_conf="jetson-xavier-rqx580",
            ),
            "2888-400-0004-M.0-1-2-jetson-xavier-rqx580-",
            False,
        ),
    ),
)
def test_check_compat(_spec, _compat_str, _expected):
    assert _spec.check_compat(_compat_str) is _expected


EXAMPLE_FIRMWARE_MANIFEST = """\
format_version: 1
hardware_type: nvidia_jetson
hardware_series: ADLINK RQX
hardware_model: rqx580
firmware_spec:
  bsp_version: r35.4.1
  firmware_compat:
    board_id: "2888"
    board_sku: "0004"
    board_rev: L.0
    fab_id: "400"
    board_conf: jetson-xavier-rqx580
firmware_packages:
  - payload_name: bl_only_payload.Cap
    file_location: file:///opt/ota/firmware/bl_only_payload.Cap
    type: UEFI-CAPSULE
    digest: "sha256:32baa6f7e96661d50fb78e5d7149763e3a0fe70c51c37c6bea92c3c27cd2472d"
  - payload_name: BOOTAA64.efi
    file_location: file:///opt/ota/firmware/BOOTAA64.efi
    type: UEFI-BOOT-APP
    digest: "sha256:ac17457772666351154a5952e3b87851a6398da2afcf3a38bedfc0925760bb0e"
"""

EXAMPLE_FIRMWARE_MANIFEST_PARSED = FirmwareManifest(
    format_version=1,
    hardware_type=HardwareType("nvidia_jetson"),
    hardware_series="ADLINK RQX",
    hardware_model="rqx580",
    firmware_spec=NVIDIAUEFIFirmwareSpec(
        bsp_version="r35.4.1",
        firmware_compat=NVIDIAFirmwareCompat(
            board_id="2888",
            board_sku="0004",
            board_rev="L.0",
            fab_id="400",
            board_conf="jetson-xavier-rqx580",
        ),
    ),
    firmware_packages=[
        FirmwarePackage(
            payload_name="bl_only_payload.Cap",
            file_location=PayloadFileLocation(
                "file:///opt/ota/firmware/bl_only_payload.Cap"
            ),
            type=PayloadType("UEFI-CAPSULE"),
            digest=DigestValue(
                "sha256:32baa6f7e96661d50fb78e5d7149763e3a0fe70c51c37c6bea92c3c27cd2472d"
            ),
        ),
        FirmwarePackage(
            payload_name="BOOTAA64.efi",
            file_location=PayloadFileLocation("file:///opt/ota/firmware/BOOTAA64.efi"),
            type=PayloadType("UEFI-BOOT-APP"),
            digest=DigestValue(
                "sha256:ac17457772666351154a5952e3b87851a6398da2afcf3a38bedfc0925760bb0e"
            ),
        ),
    ],
)


@pytest.mark.parametrize(
    "_in, _expected",
    (
        (
            EXAMPLE_FIRMWARE_MANIFEST,
            EXAMPLE_FIRMWARE_MANIFEST_PARSED,
        ),
    ),
)
def test_firmware_manifest_parsing(_in, _expected):
    assert FirmwareManifest.model_validate(yaml.safe_load(_in)) == _expected


EXAMPLE_FIRMWARE_UPDATE_REQUEST = """\
format_version: 1
firmware_list:
  - bl_only_payload.Cap
  - BOOTAA64.efi
"""

EXAMPLE_FIRMWARE_UPDATE_REQUEST_PARSED = FirmwareUpdateRequest(
    format_version=1, firmware_list=["bl_only_payload.Cap", "BOOTAA64.efi"]
)


@pytest.mark.parametrize(
    "_in, _expected",
    (
        (
            EXAMPLE_FIRMWARE_UPDATE_REQUEST,
            EXAMPLE_FIRMWARE_UPDATE_REQUEST_PARSED,
        ),
    ),
)
def test_firmware_update_request_parsing(_in, _expected):
    assert FirmwareUpdateRequest.model_validate(yaml.safe_load(_in)) == _expected
