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
"""Finding otaclient's own release package in an OTA image version 1.

A campaign updates the client before the rootfs, so that a client too old to read the
new image — or too broken to install it — can still be replaced. These tests are about
what the selector hands the download helper, because that is what went wrong the first
time it was ever called: the helper takes a batch of downloads per step, and a bare
descriptor dispatches nothing, leaving the client update to wait for ever.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, List, Union

import pytest

from ota_metadata.v1 import OTAImageHelper
from otaclient_common.download_info import DownloadInfo

MANIFEST_FNAME = "otaclient_release_manifest.json"


class _FakeDescriptor:
    """Stands in for the descriptors the index lists."""

    def __init__(self, digest_hex: str, size: int = 1) -> None:
        self.digest = type("_D", (), {"digest_hex": digest_hex})()
        self.size = size


class _FakeIndex:
    def __init__(
        self, descriptors: List[Any], legacy_descriptors: List[Any] | None = None
    ) -> None:
        self._descriptors = descriptors
        self._legacy_descriptors = legacy_descriptors or []

    def find_update_agent_package(self) -> Any:
        return self._descriptors[0] if self._descriptors else None

    def find_otaclient_package(self) -> List[Any]:
        return self._legacy_descriptors


@pytest.fixture
def image_helper(tmp_path: Path) -> OTAImageHelper:
    return OTAImageHelper(
        session_dir=tmp_path,
        base_url="http://ota.example/",
        ca_store={},  # type: ignore[arg-type]  # nothing here verifies a signature
    )


def _release_manifest(
    version: str = "1.2.3", agent_type: str = "tier4.otaclient.squashfs.v1"
) -> str:
    """The manifest as `add-update-agent-package` writes it into the image: an update
    agent release package whose layers are the bundles, annotated with type, version
    and architecture."""
    _digest = "sha256:" + "a" * 64
    return json.dumps(
        {
            "schemaVersion": 1,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "artifactType": "application/vnd.tier4.ota.update-agent.release-package.v1",
            "layers": [
                {
                    "size": 4,
                    "digest": _digest,
                    "mediaType": "application/vnd.tier4.ota.update-agent.bundle.v1",
                    "annotations": {
                        "vnd.tier4.ota.update-agent.type": agent_type,
                        "vnd.tier4.ota.update-agent.version": version,
                        "vnd.tier4.ota.update-agent.architecture": "x86_64",
                    },
                }
            ],
        }
    )


def _drive(gen, tmp_path: Path, *, manifest: Union[str, List[str]]) -> List[Any]:
    """Consume the generator the way the download helper does.

    The generator holds the condition while suspended at a yield and waits on it after
    being resumed, so the notify has to come from another thread — in production, from
    the worker that finished the download. `manifest` is what each manifest download
    in turn brings; one string serves every step.
    """
    _manifests = [manifest] if isinstance(manifest, str) else manifest
    _steps: List[Any] = []
    _stop = threading.Event()

    def _worker() -> None:
        # Stand in for the downloads: the manifest the current step asked for arrives,
        # then the generator is woken so it can go on to what that manifest names.
        while not _stop.wait(0.02):
            with gen.gi_frame.f_locals["condition"]:
                _n = min(max(len(_steps), 1), len(_manifests)) - 1
                (tmp_path / MANIFEST_FNAME).write_text(_manifests[_n])
                gen.gi_frame.f_locals["condition"].notify_all()

    _t = threading.Thread(target=_worker, daemon=True)
    _t.start()
    try:
        for _step in gen:
            _steps.append(_step)
    finally:
        _stop.set()
        _t.join(timeout=2)
    return _steps


def test_every_step_hands_the_helper_a_batch(image_helper, tmp_path, monkeypatch):
    """`download_meta_files` iterates what it is given, so each step is a list. A bare
    DownloadInfo dispatches no download and the step after it never runs — a client
    update that simply hangs, which is how this was found."""
    monkeypatch.setattr("ota_metadata.v1._get_arch", lambda: "x86_64")
    image_helper.image_index = _FakeIndex([_FakeDescriptor("d" * 64)])

    _steps = _drive(
        image_helper.select_otaclient_package(
            tmp_path / "otaclient.squashfs", "1.2.3", condition=threading.Condition()
        ),
        tmp_path,
        manifest=_release_manifest(),
    )

    assert len(_steps) == 2, "the release manifest first, then the package it names"
    for _step in _steps:
        assert isinstance(_step, list), "the helper takes a batch per step"
        assert all(isinstance(_i, DownloadInfo) for _i in _step)


def _legacy_release_manifest(version: str = "1.2.3") -> str:
    """The OTAClient release package as an image built before the update agent
    release package existed carries it."""
    _digest = "sha256:" + "a" * 64
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "artifactType": "application/vnd.tier4.otaclient.release-package.v1",
            "config": {
                "size": 2,
                "digest": "sha256:" + "c" * 64,
                "mediaType": "application/vnd.tier4.otaclient.release-package.manifest.v1+json",
            },
            "layers": [
                {
                    "size": 4,
                    "digest": _digest,
                    "mediaType": "application/vnd.tier4.otaclient.release-package.v1.squashfs",
                    "annotations": {
                        "version": version,
                        "type": "squashfs",
                        "architecture": "x86_64",
                        "size": 4,
                        "checksum": _digest,
                    },
                }
            ],
            "annotations": {"date": "2026-01-01"},
        }
    )


def test_an_image_from_before_the_update_agent_package_still_updates_the_client(
    image_helper, tmp_path, monkeypatch
):
    """A client at this version must still take its package from an image that lists
    it the old way: the images already built are not rebuilt."""
    monkeypatch.setattr("ota_metadata.v1._get_arch", lambda: "x86_64")
    image_helper.image_index = _FakeIndex(
        [], legacy_descriptors=[_FakeDescriptor("e" * 64)]
    )

    _steps = _drive(
        image_helper.select_otaclient_package(
            tmp_path / "otaclient.squashfs", "1.2.3", condition=threading.Condition()
        ),
        tmp_path,
        manifest=_legacy_release_manifest(),
    )

    assert len(_steps) == 2, "the legacy manifest first, then the package it names"


def test_an_update_agent_package_of_other_agents_does_not_hide_the_client_package(
    image_helper, tmp_path, monkeypatch
):
    """An image whose update agent release package lists the partition agent keeps
    otaclient in the OTAClient release package, the entry every client reads; the
    client's package is found there, after the newer entry has been looked at."""
    monkeypatch.setattr("ota_metadata.v1._get_arch", lambda: "x86_64")
    image_helper.image_index = _FakeIndex(
        [_FakeDescriptor("d" * 64)], legacy_descriptors=[_FakeDescriptor("e" * 64)]
    )

    _steps = _drive(
        image_helper.select_otaclient_package(
            tmp_path / "otaclient.squashfs", "1.2.3", condition=threading.Condition()
        ),
        tmp_path,
        manifest=[
            _release_manifest("1.2.3", agent_type="tier4.ota.agent.v1"),
            _legacy_release_manifest("1.2.3"),
        ],
    )

    assert len(_steps) == 3, "both manifests, then the package the legacy one names"
    assert _steps[0][0].url.endswith("d" * 64), "the update agent release package first"
    assert _steps[1][0].url.endswith("e" * 64), "then the OTAClient release package"
    assert _steps[2][0].url.endswith("a" * 64), "the package it names"


def test_an_image_without_a_package_asks_for_nothing(
    image_helper, tmp_path, monkeypatch
):
    monkeypatch.setattr("ota_metadata.v1._get_arch", lambda: "x86_64")
    image_helper.image_index = _FakeIndex([])

    _gen = image_helper.select_otaclient_package(
        tmp_path / "otaclient.squashfs", "1.2.3", condition=threading.Condition()
    )

    assert list(_gen) == []


def test_a_version_the_image_does_not_carry_stops_after_the_manifest(
    image_helper, tmp_path, monkeypatch
):
    """Nothing is downloaded for a version that is not there, and the client that asked
    stays the client that runs."""
    monkeypatch.setattr("ota_metadata.v1._get_arch", lambda: "x86_64")
    image_helper.image_index = _FakeIndex([_FakeDescriptor("d" * 64)])

    _steps = _drive(
        image_helper.select_otaclient_package(
            tmp_path / "otaclient.squashfs", "9.9.9", condition=threading.Condition()
        ),
        tmp_path,
        manifest=_release_manifest("1.2.3"),
    )

    assert len(_steps) == 1, "the manifest, and then nothing to fetch"
