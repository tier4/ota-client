# Copyright 2026 TIER IV, INC. All rights reserved.
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
"""Selecting this ECU's payload from a version 1 image index, file by file.

An index may list payloads of both kinds (spec/partition_image.md): a consumer that
rebuilds the standby slot from files must tell a partition-based payload for its ECU
apart from an index that carries nothing for it, and refuse the former as what it is.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from ota_image_libs.v1.image_index.schema import ImageIndex
from ota_image_libs.v1.image_manifest.schema import (
    ImageIdentifier,
    ImageManifest,
    OTAReleaseKey,
)
from ota_image_libs.v1.media_types import IMAGE_MANIFEST, PARTITION_IMAGE_ARTIFACT
from ota_image_libs.v1.partition_image.schema import PartitionImageManifest

from ota_metadata.errors import ImageMetadataInvalid
from ota_metadata.utils.cert_store import CAStoreMap
from ota_metadata.v1 import OTAImageHelper

THIS_ECU = ImageIdentifier("autoware", OTAReleaseKey.dev)


def _descriptor(cls, *, ecu: str, artifact_type: str | None = None):
    _raw = {
        "mediaType": IMAGE_MANIFEST,
        "digest": f"sha256:{'1' * 64}",
        "size": 1210,
        "annotations": {
            "vnd.tier4.pilot-auto.platform.ecu": ecu,
            "vnd.tier4.ota.release-key": "dev",
        },
    }
    if artifact_type is not None:
        _raw["artifactType"] = artifact_type
    return cls.model_validate(_raw)


def file_based_payload(ecu: str) -> ImageManifest.Descriptor:
    return _descriptor(ImageManifest.Descriptor, ecu=ecu)


def partition_based_payload(ecu: str) -> PartitionImageManifest.Descriptor:
    return _descriptor(
        PartitionImageManifest.Descriptor,
        ecu=ecu,
        artifact_type=PARTITION_IMAGE_ARTIFACT,
    )


def helper_with(tmp_path: Path, *manifests) -> OTAImageHelper:
    _helper = OTAImageHelper(
        session_dir=tmp_path,
        base_url="http://ota.example/image/",
        ca_store=CAStoreMap(),
    )
    _helper.image_index = ImageIndex(
        manifests=list(manifests),
        annotations={"vnd.tier4.ota.ota-image-builder.version": "0.16.0"},
    )
    return _helper


def select(helper: OTAImageHelper):
    """Drive the selection up to its first download request."""
    return next(helper.select_image_payload(THIS_ECU, threading.Condition()))


def test_a_partition_based_payload_for_this_ecu_is_refused_as_such(tmp_path: Path):
    """Not "cannot be found": the payload is there, and it is not one a slot rebuilt
    from files can take."""
    _helper = helper_with(tmp_path, partition_based_payload("autoware"))

    with pytest.raises(ImageMetadataInvalid, match="partition-based"):
        select(_helper)


def test_an_image_with_nothing_for_this_ecu_is_still_not_found(tmp_path: Path):
    _helper = helper_with(
        tmp_path, file_based_payload("perception"), partition_based_payload("main")
    )

    with pytest.raises(ImageMetadataInvalid, match="cannot be found"):
        select(_helper)


def test_this_ecus_file_based_payload_is_selected_beside_other_kinds(tmp_path: Path):
    """Another ECU's partition-based payload in the same index changes nothing."""
    _helper = helper_with(
        tmp_path, partition_based_payload("main"), file_based_payload("autoware")
    )

    [_download] = select(_helper)

    assert _download.url.endswith(f"/blobs/sha256/{'1' * 64}")
