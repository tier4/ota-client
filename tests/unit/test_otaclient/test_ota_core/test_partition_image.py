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
"""Deciding what a partition-based payload costs this device, fetching that, and
handing it to the installer.

The decision is the whole point of these tests: a delta applies to bytes named by
digest, the device is the only thing that knows whether it holds them, and the answer
has to be reached before a gigabyte is downloaded rather than after.
"""

from __future__ import annotations

import shutil
import threading
from contextlib import contextmanager
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from typing import List, Optional, Tuple

import pytest
from ota_image_libs.v1.consts import (
    IMAGE_INDEX_FNAME,
    INDEX_JWT_FNAME,
    RESOURCE_DIR,
)
from ota_image_libs.v1.image_index.schema import ImageIndex
from ota_image_libs.v1.image_manifest.schema import (
    ImageIdentifier,
    OTAReleaseKey,
)
from ota_image_libs.v1.media_types import PARTITION_IMAGE_BLOB, PARTITION_IMAGE_DELTA
from ota_image_libs.v1.partition_image.schema import (
    DELTA_ALGORITHM_BLOCK_DIFF,
    BootFilesDescriptor,
    DataImageBlobDescriptor,
    DataImageEntry,
    DeliveryMode,
    PartitionAction,
    PartitionDeltaDescriptor,
    PartitionEntry,
    PartitionImageBlobDescriptor,
    PartitionImageConfig,
    PartitionImageManifest,
    VendorPackageDescriptor,
)

from ota_metadata.utils.cert_store import CAStoreMap
from otaclient import errors as ota_errors
from otaclient.boot_control.protocol import PartitionInstallError
from otaclient.ota_core._partition_image import (
    OTAUpdaterForPartitionImage,
    PartitionImageUpdater,
)
from otaclient_common.downloader import HashVerificationError

PARTITION_BLOB_MEDIA = PARTITION_IMAGE_BLOB
PARTITION_DELTA_MEDIA = PARTITION_IMAGE_DELTA
BOOT_FILES_MEDIA = (
    "application/vnd.tier4.ota.partition-based-ota-image.boot-files.v1.tar"
)
VENDOR_PACKAGE_MEDIA = (
    "application/vnd.tier4.ota.partition-based-ota-image.vendor-package.v1"
)

BASE_URL = "http://ota.example/image/"
THIS_ECU = ImageIdentifier("autoware", OTAReleaseKey.dev)

ON_DEVICE = "a" * 64
"""The digest of what the committed slot holds, in these tests."""
SOMETHING_ELSE = "b" * 64


def blob(digest: str, size: int = 4096) -> PartitionImageBlobDescriptor:
    return PartitionImageBlobDescriptor(
        mediaType=PARTITION_BLOB_MEDIA, digest=f"sha256:{digest}", size=size
    )


def boot_files(digest: str, size: int = 512) -> BootFilesDescriptor:
    return BootFilesDescriptor(
        mediaType=BOOT_FILES_MEDIA, digest=f"sha256:{digest}", size=size
    )


def delta_against(source: str, *, size: int = 100) -> PartitionDeltaDescriptor:
    return PartitionDeltaDescriptor(
        mediaType=PARTITION_DELTA_MEDIA,
        digest=f"sha256:{'d' * 64}",
        size=size,
        annotations={
            "vnd.tier4.ota.partition-image.delta.algorithm": DELTA_ALGORITHM_BLOCK_DIFF,
            "vnd.tier4.ota.partition-image.delta.source-digest": f"sha256:{source}",
            "vnd.tier4.ota.partition-image.delta.source-size": 4096,
        },
    )


def config(
    *partitions: PartitionEntry,
    delivery: DeliveryMode = DeliveryMode.direct,
    data_images: tuple[DataImageEntry, ...] = (),
):
    """A payload config, built directly: what the updater reads, without the
    downloading that puts it there."""
    _kwargs = dict(
        architecture="x86_64",
        image_version="2.9.0",
        delivery=delivery,
        partitions=list(partitions),
        data_images=list(data_images),
        labels={
            "vnd.tier4.image.base-image": "ubuntu:24.04",
            "vnd.tier4.ota.image.blobs-count": 2,
            "vnd.tier4.ota.image.blobs-size": 4608,
        },
    )
    if delivery is DeliveryMode.vendor_package:
        _kwargs["package"] = VendorPackageDescriptor(
            mediaType=VENDOR_PACKAGE_MEDIA, digest=f"sha256:{'e' * 64}", size=10
        )
    return PartitionImageConfig(**_kwargs)


def data_image(
    *, delta: Optional[PartitionDeltaDescriptor] = None, name: str = "ml_package"
) -> DataImageEntry:
    """A data image beside the partitions: a model set the device keeps as a file."""
    return DataImageEntry(
        name=name,
        version="2026.9.1",
        mount="/opt/autoware/ml",
        image=DataImageBlobDescriptor(
            mediaType="application/vnd.tier4.ota.partition-based-ota-image.data-image.v1",
            digest=f"sha256:{'d' * 64}",
            size=209715200,
        ),
        delta=delta,
    )


def rootfs(*, delta: Optional[PartitionDeltaDescriptor] = None) -> PartitionEntry:
    return PartitionEntry(
        name="rootfs",
        action=PartitionAction.write,
        image=blob("f" * 64, size=1534529536),
        delta=delta,
    )


def boot() -> PartitionEntry:
    return PartitionEntry(
        name="boot", action=PartitionAction.write, image=boot_files("c" * 64)
    )


class FakeDownloader:
    """Records what was asked for, and puts bytes where they were asked to go.

    With `served`, it serves that directory as if it were the OTA image's URL base,
    which is what makes the metadata tests read a real index, manifest and config
    instead of ones handed to the updater directly.
    """

    def __init__(
        self, served: Optional[Path] = None, fail_with: Optional[Exception] = None
    ) -> None:
        self.calls: List[Tuple[str, Path]] = []
        self._served = served
        self._fail_with = fail_with

    def download(self, url, dst, *, digest=None, size=None, **kwargs):
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        self.calls.append((url, dst))
        if self._fail_with is not None:
            raise self._fail_with
        if self._served is not None:
            _name = url.split("/image/", 1)[1]
            _src = self._served / _name
            if not _src.is_file():
                raise FileNotFoundError(f"404: {url}")
            dst.write_bytes(_src.read_bytes())
            return
        dst.write_bytes(b"\0" * (size or 1))


class FakeInstaller:
    """The device side: what it holds, and what it was handed."""

    def __init__(
        self,
        *,
        on_device: str = ON_DEVICE,
        digest_error: bool = False,
        install_error: bool = False,
    ) -> None:
        self._on_device = on_device
        self._digest_error = digest_error
        self._install_error = install_error
        self.installed: Optional[dict] = None
        self.blobs_seen: list[str] = []
        self.asked: list[tuple[int, Optional[str]]] = []
        self.data_on_device: dict[str, str] = {}

    def source_digest(self, *, size: int, data_image: Optional[str] = None) -> str:
        if self._digest_error:
            raise PartitionInstallError("cannot read the slot")
        self.asked.append((size, data_image))
        if data_image is not None:
            return self.data_on_device.get(data_image, "0" * 64)
        return self._on_device

    def install(self, *, package, on_progress=None, **kwargs):
        if self._install_error:
            raise PartitionInstallError("the payload was refused")
        self.installed = dict(package=Path(package), **kwargs)
        _blob_dir = Path(package) / RESOURCE_DIR
        self.blobs_seen = sorted(p.name for p in _blob_dir.iterdir())
        if on_progress is not None:
            for _percent in (0, 50, 100):
                on_progress(_percent)
        return True


def make_updater(
    tmp_path: Path,
    installer: Optional[FakeInstaller] = None,
    served: Optional[Path] = None,
):
    return PartitionImageUpdater(
        base_url=BASE_URL,
        downloader=FakeDownloader(served),
        session_dir=tmp_path,
        image_id=THIS_ECU,
        installer=installer or FakeInstaller(),
    )


def serve_an_image(root: Path, payload_config: PartitionImageConfig, *, ecu="autoware"):
    """An OTA image as it is published: index.json, index.jwt and the blobs.

    Built with the same models the updater parses, so a field that changes shape
    breaks these tests rather than a campaign. The payload's blobs are there under
    their digests, with whatever bytes: the fake downloader checks nothing.
    """
    _blobs = root / RESOURCE_DIR
    _blobs.mkdir(parents=True, exist_ok=True)

    _config_desc = PartitionImageConfig.Descriptor.export_metafile_to_resource_dir(
        payload_config, _blobs
    )
    _manifest = PartitionImageManifest(
        config=_config_desc,
        layers=payload_config.payload_descriptors,
        annotations={
            "vnd.tier4.pilot-auto.platform.ecu": ecu,
            "vnd.tier4.pilot-auto.platform.ecu.architecture": "x86_64",
        },
    )
    _manifest_desc = PartitionImageManifest.Descriptor.export_metafile_to_resource_dir(
        _manifest,
        _blobs,
        annotations={
            "vnd.tier4.pilot-auto.platform.ecu": ecu,
            "vnd.tier4.ota.release-key": "dev",
        },
    )
    for _layer in payload_config.payload_descriptors:
        _blob = _blobs / _layer.digest.digest_hex
        if not _blob.exists():
            _blob.write_bytes(b"\0" * min(_layer.size, 4096))
    _index = ImageIndex(
        manifests=[_manifest_desc],
        annotations={"vnd.tier4.ota.ota-image-builder.version": "0.6.0"},
    )
    (root / IMAGE_INDEX_FNAME).write_text(_index.export_metafile())
    # The signature is verified by the updater's index verification, which these
    # tests stand in for: the bytes only have to travel into the unpacked image.
    (root / INDEX_JWT_FNAME).write_text("a-signature-over-the-index")
    return root


def read_metadata(updater: PartitionImageUpdater, served: Path):
    """What `OTAUpdaterForPartitionImage` does once the index is verified: hand the
    parsed index and the two index files to the updater."""
    _index = ImageIndex.parse_metafile((served / IMAGE_INDEX_FNAME).read_text())
    return updater.download_metadata(
        _index,
        index_json=served / IMAGE_INDEX_FNAME,
        index_jwt=served / INDEX_JWT_FNAME,
    )


class TestDownloadMetadata:
    def test_it_reads_the_payload_for_this_ecu(self, tmp_path: Path):
        _served = serve_an_image(
            tmp_path / "served", config(rootfs(delta=delta_against(ON_DEVICE)))
        )
        _updater = make_updater(tmp_path / "session", served=_served)

        _config = read_metadata(_updater, _served)

        assert _config.image_version == "2.9.0"
        assert _config.delivery is DeliveryMode.direct
        assert [_p.name for _p in _config.partitions] == ["rootfs"]

    def test_the_metadata_lands_where_the_installer_reads_it(self, tmp_path: Path):
        """index.json and index.jwt go in unchanged, beside the blobs: the installer
        verifies the signature again on the device, so otaclient hands it the bytes
        it was given rather than anything it re-derived."""
        _served = serve_an_image(tmp_path / "served", config(rootfs()))
        _updater = make_updater(tmp_path / "session", served=_served)

        read_metadata(_updater, _served)

        _image = tmp_path / "session" / "image"
        assert (_image / IMAGE_INDEX_FNAME).read_bytes() == (
            _served / IMAGE_INDEX_FNAME
        ).read_bytes()
        assert (_image / INDEX_JWT_FNAME).read_bytes() == (
            _served / INDEX_JWT_FNAME
        ).read_bytes()
        # the manifest and the config, by digest, and nothing else yet
        assert len(list((_image / RESOURCE_DIR).iterdir())) == 2

    def test_an_image_without_a_payload_for_this_ecu_is_refused(self, tmp_path: Path):
        _served = serve_an_image(
            tmp_path / "served", config(rootfs()), ecu="perception"
        )
        _updater = make_updater(tmp_path / "session", served=_served)

        with pytest.raises(ota_errors.OTAImageInvalid, match="no partition-based"):
            read_metadata(_updater, _served)

    def test_metadata_then_plan_reads_end_to_end(self, tmp_path: Path):
        """The two halves together: what the image says, and what this device needs
        of it."""
        _served = serve_an_image(
            tmp_path / "served",
            config(rootfs(delta=delta_against(ON_DEVICE))),
        )
        _updater = make_updater(tmp_path / "session", served=_served)

        read_metadata(_updater, _served)
        _plan = _updater.plan()

        assert [_b.what for _b in _plan.blobs] == ["the rootfs delta"]


class TestPlan:
    def test_a_delta_that_fits_is_what_gets_downloaded(self, tmp_path: Path):
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)), boot())

        _plan = _updater.plan()

        assert [_b.what for _b in _plan.blobs] == ["the rootfs delta", "the boot image"]
        assert _plan.total_size == 100 + 512

    def test_a_delta_that_does_not_fit_is_refused_before_downloading(
        self, tmp_path: Path
    ):
        """Refused before downloading, not after: the payload cannot produce the
        bytes this device would need."""
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(SOMETHING_ELSE)))

        with pytest.raises(ota_errors.ApplyOTAUpdateFailed, match="not at the version"):
            _updater.plan()

    def test_an_installer_that_cannot_answer_is_a_refusal(self, tmp_path: Path):
        """No answer is not "it fits": a delta is only ever applied to bytes the
        device has been seen to hold."""
        _updater = make_updater(tmp_path, FakeInstaller(digest_error=True))
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))

        with pytest.raises(ota_errors.ApplyOTAUpdateFailed):
            _updater.plan()

    def test_partitions_that_carry_nothing_are_not_downloads(self, tmp_path: Path):
        """mkfs makes an empty filesystem and keep touches nothing; neither has bytes
        on the wire."""
        _updater = make_updater(tmp_path)
        _updater.image_config = config(
            rootfs(),
            PartitionEntry(name="scratch", action=PartitionAction.mkfs),
            PartitionEntry(name="identity", action=PartitionAction.keep),
            PartitionEntry(name="optdata", action=PartitionAction.keep),
        )

        assert [_b.what for _b in _updater.plan().blobs] == ["the rootfs image"]

    def test_a_data_image_is_planned_beside_the_partitions(self, tmp_path: Path):
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(), boot(), data_images=(data_image(),))
        _plan = _updater.plan()
        assert [_b.what for _b in _plan.blobs] == [
            "the rootfs image",
            "the boot image",
            "the ml_package data image",
        ]

    def test_a_data_image_delta_is_planned_when_the_device_holds_its_source(
        self, tmp_path: Path
    ):
        _installer = FakeInstaller()
        _installer.data_on_device["ml_package"] = ON_DEVICE
        _updater = make_updater(tmp_path, installer=_installer)
        _updater.image_config = config(
            PartitionEntry(name="rootfs", action=PartitionAction.keep),
            PartitionEntry(name="boot", action=PartitionAction.keep),
            data_images=(data_image(delta=delta_against(ON_DEVICE)),),
        )
        _plan = _updater.plan()
        assert [_b.what for _b in _plan.blobs] == ["the ml_package data image delta"]
        # asked of the data image's file, not of the rootfs slot
        assert _installer.asked[-1][1] == "ml_package"

    def test_a_data_image_delta_that_does_not_fit_is_refused(self, tmp_path: Path):
        _installer = FakeInstaller()
        _installer.data_on_device["ml_package"] = "9" * 64
        _updater = make_updater(tmp_path, installer=_installer)
        _updater.image_config = config(
            PartitionEntry(name="rootfs", action=PartitionAction.keep),
            PartitionEntry(name="boot", action=PartitionAction.keep),
            data_images=(data_image(delta=delta_against(ON_DEVICE)),),
        )
        with pytest.raises(
            ota_errors.ApplyOTAUpdateFailed, match="ml_package data image delta"
        ):
            _updater.plan()

    def test_a_vendor_package_is_fetched_whole_and_applied_by_the_platform(
        self, tmp_path: Path
    ):
        """otaclient fetches the package; the platform's own updater applies it.
        There is nothing to choose between and no partition to size against, so
        the plan is that one blob."""
        _updater = make_updater(tmp_path)
        _updater.image_config = config(
            PartitionEntry(
                name="rootfs",
                action=PartitionAction.write,
                performed_by="package",
            ),
            delivery=DeliveryMode.vendor_package,
        )

        _plan = _updater.plan()

        assert [_b.what for _b in _plan.blobs] == ["the vendor package"]
        assert _plan.total_size == 10

    def test_planning_before_reading_the_metadata_is_a_bug(self, tmp_path: Path):
        with pytest.raises(ValueError):
            make_updater(tmp_path).plan()


class TestDownloadAndApply:
    def test_only_the_planned_blobs_are_fetched_and_where_the_installer_reads_them(
        self, tmp_path: Path
    ):
        """The unpacked image holds exactly what this device needs."""
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))

        _image_dir = _updater.download_payload(_updater.plan())

        assert _image_dir == tmp_path / "image"
        _fetched = sorted(p.name for p in (_image_dir / "blobs/sha256").iterdir())
        assert _fetched == ["d" * 64]  # the delta, not the image beside it
        _urls = [url for url, _ in _updater._downloader.calls]
        assert _urls == [f"{BASE_URL}blobs/sha256/{'d' * 64}"]

    def test_a_blob_left_by_an_earlier_attempt_is_reused(
        self, tmp_path: Path, monkeypatch
    ):
        """An interrupted update should not pay for its downloads twice."""
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))
        _plan = _updater.plan()
        _delta = _plan.blobs[0]
        _dst = tmp_path / "image" / RESOURCE_DIR / _delta.digest_hex
        _dst.parent.mkdir(parents=True)
        _dst.write_bytes(b"x" * _delta.size)  # whatever hashes to that digest

        import otaclient.ota_core._partition_image as _mod

        # stand in for hashing them: these bytes are the ones the descriptor names
        monkeypatch.setattr(_mod, "_digest_of", lambda _p: _delta.digest_hex)
        _updater.download_payload(_plan)

        assert _updater._downloader.calls == []

    def test_a_blob_of_the_right_length_but_the_wrong_bytes_is_downloaded_again(
        self, tmp_path: Path
    ):
        """A truncated write or a bad disk leaves exactly that, and the next thing to
        read it writes it onto a partition."""
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))
        _plan = _updater.plan()
        _delta = _plan.blobs[0]
        _dst = tmp_path / "image" / RESOURCE_DIR / _delta.digest_hex
        _dst.parent.mkdir(parents=True)
        _dst.write_bytes(b"x" * _delta.size)

        _updater.download_payload(_plan)  # the real digest of those bytes is not it

        assert len(_updater._downloader.calls) == 1

    def test_a_half_downloaded_blob_is_downloaded_again(self, tmp_path: Path):
        """What an interrupted download left is not a blob: it is fetched again from
        the start into the same place, and only the whole, verified file carries the
        digest as its name."""
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))
        _plan = _updater.plan()
        _delta = _plan.blobs[0]
        _part = tmp_path / "image" / RESOURCE_DIR / f"{_delta.digest_hex}.part"
        _part.parent.mkdir(parents=True)
        _part.write_bytes(b"x" * (_delta.size // 2))

        _updater.download_payload(_plan)

        assert [_dst for _, _dst in _updater._downloader.calls] == [_part]
        _blob = tmp_path / "image" / RESOURCE_DIR / _delta.digest_hex
        assert _blob.is_file() and _blob.stat().st_size == _delta.size
        assert not _part.exists()

    def test_bytes_that_do_not_hash_to_the_digest_are_removed(self, tmp_path: Path):
        """A whole file that hashes to something else is not this blob, and nothing
        is left that the next attempt could take for one."""
        _updater = make_updater(tmp_path)
        _updater._downloader = FakeDownloader(fail_with=HashVerificationError("nope"))
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))
        _plan = _updater.plan()
        _delta = _plan.blobs[0]
        _part = tmp_path / "image" / RESOURCE_DIR / f"{_delta.digest_hex}.part"
        _part.parent.mkdir(parents=True)
        _part.write_bytes(b"x" * (_delta.size // 2))

        with pytest.raises(HashVerificationError):
            _updater.download_payload(_plan)

        assert not _part.exists()

    def test_a_device_without_room_is_told_before_the_download_starts(
        self, tmp_path: Path, monkeypatch
    ):
        """Partway through is where it would otherwise be found: after minutes of
        downloading, and reported as a failure to apply rather than as a device with
        no room."""
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))
        _plan = _updater.plan()

        import otaclient.ota_core._partition_image as _mod

        monkeypatch.setattr(
            _mod.shutil,
            "disk_usage",
            lambda _p: SimpleNamespace(free=_plan.total_size - 1),
        )
        with pytest.raises(ota_errors.StandbySlotInsufficientSpace, match="free"):
            _updater.download_payload(_plan)

        assert _updater._downloader.calls == []

    def test_what_is_already_staged_does_not_count_against_the_room_needed(
        self, tmp_path: Path, monkeypatch
    ):
        """A retry has most of the payload on disk already; requiring room for all of
        it again would refuse an update that fits."""
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))
        _plan = _updater.plan()
        _delta = _plan.blobs[0]
        _dst = tmp_path / "image" / RESOURCE_DIR / _delta.digest_hex
        _dst.parent.mkdir(parents=True)
        _dst.write_bytes(b"x" * _delta.size)

        import otaclient.ota_core._partition_image as _mod

        monkeypatch.setattr(_mod, "_digest_of", lambda _p: _delta.digest_hex)
        monkeypatch.setattr(
            _mod.shutil, "disk_usage", lambda _p: SimpleNamespace(free=_delta.size)
        )
        _updater.download_payload(_plan)  # room for what is left, which is nothing

    def test_apply_hands_the_unpacked_image_to_the_installer(self, tmp_path: Path):
        _installer = FakeInstaller()
        _updater = make_updater(tmp_path, _installer)
        (tmp_path / "image" / RESOURCE_DIR).mkdir(parents=True)

        assert _updater.apply(version="2.9.0") is True

        assert _installer.installed is not None
        assert _installer.installed["package"] == tmp_path / "image"
        assert _installer.installed["version"] == "2.9.0"
        # which payload of a multi-ECU image: the one otaclient downloaded
        assert _installer.installed["ecu_id"] == "autoware"
        assert _installer.installed["release_key"] == "dev"

    def test_a_refused_payload_is_a_failed_update(self, tmp_path: Path):
        _updater = make_updater(tmp_path, FakeInstaller(install_error=True))
        with pytest.raises(ota_errors.ApplyOTAUpdateFailed):
            _updater.apply(version="2.9.0")


#
# ------ the updater otaclient runs ------ #
#


class FakeBootController:
    """What the update asks of boot control, recorded."""

    def __init__(self) -> None:
        self.rebooted = False
        self.pre_updated: Optional[tuple[bool, bool]] = None
        self.post_updated: Optional[tuple[str, object]] = None
        self.failed = False

    def pre_update(self, *, standby_as_ref: bool, erase_standby: bool) -> None:
        self.pre_updated = (standby_as_ref, erase_standby)

    def post_update(self, update_version, *, version_detail=None, **kwargs) -> None:
        self.post_updated = (update_version, version_detail)

    def on_operation_failure(self) -> None:
        self.failed = True

    def finalizing_update(self, *, chroot=None) -> None:
        self.rebooted = True


class FakeAbortHandler:
    state = None

    @contextmanager
    def critical_zone(self):
        yield

    def enter_final_phase(self) -> None:
        pass


def make_full_updater(
    tmp_path: Path,
    *,
    child_in_update: Optional[threading.Event] = None,
    installer: Optional[FakeInstaller] = None,
    served: Optional[Path] = None,
):
    """OTAUpdaterForPartitionImage with otaclient's own machinery, minus the parts a
    single ECU in a test cannot have: the shared-memory metrics reader, the abort
    handler and the downloader pool's threads."""
    from otaclient._types import MultipleECUStatusFlags
    from otaclient.metrics import OTAMetricsData

    _flags = MultipleECUStatusFlags(
        any_child_ecu_in_update=child_in_update or threading.Event(),
        any_requires_network=threading.Event(),
        all_success=threading.Event(),
    )
    return OTAUpdaterForPartitionImage(
        ca_store=CAStoreMap(),
        boot_controller=FakeBootController(),  # type: ignore[arg-type]
        abort_handler=FakeAbortHandler(),  # type: ignore[arg-type]
        image_identifier=THIS_ECU,
        installer=installer or FakeInstaller(),
        staging_dir=tmp_path / "staging",
        version="3.0.0",
        raw_url_base=BASE_URL,
        session_wd=tmp_path / "session",
        downloader_pool=SimpleNamespace(
            get_instance=lambda: FakeDownloader(served),
            release_instance=lambda: None,
            instance_num=1,
            total_downloaded_bytes=0,
        ),
        ecu_status_flags=_flags,
        status_report_queue=Queue(),
        session_id="test",
        metrics=OTAMetricsData(),
        shm_metrics_reader=None,
        release_name="",
        release_id="",
        image_id="",
    )


@pytest.fixture
def index_verified_from(monkeypatch):
    """Stand in for the index verification, which needs a signed index and a CA
    store: the served image's index files are taken as verified."""

    def _install(served: Path):
        def _verified(self: OTAUpdaterForPartitionImage):
            self._session_workdir.mkdir(parents=True, exist_ok=True)
            for _fname in (IMAGE_INDEX_FNAME, INDEX_JWT_FNAME):
                shutil.copyfile(served / _fname, self._session_workdir / _fname)
            return ImageIndex.parse_metafile((served / IMAGE_INDEX_FNAME).read_text())

        monkeypatch.setattr(
            OTAUpdaterForPartitionImage, "_verify_image_index", _verified
        )

    return _install


@pytest.fixture
def no_wait_before_reboot(monkeypatch):
    import otaclient.ota_core._updater_base as _base

    monkeypatch.setattr(_base, "WAIT_BEFORE_REBOOT", 0)


class TestExecute:
    """The whole update, from the verified index to the reboot."""

    def test_a_rootfs_release_goes_through_the_installer_to_the_reboot(
        self, tmp_path: Path, index_verified_from, no_wait_before_reboot
    ):
        _served = serve_an_image(
            tmp_path / "served",
            config(
                rootfs(),
                boot(),
                PartitionEntry(name="scratch", action=PartitionAction.mkfs),
                PartitionEntry(name="identity", action=PartitionAction.keep),
                PartitionEntry(name="optdata", action=PartitionAction.keep),
            ),
        )
        index_verified_from(_served)
        _installer = FakeInstaller()
        _updater = make_full_updater(tmp_path, installer=_installer, served=_served)

        _updater.execute()

        _boot_controller: FakeBootController = _updater._boot_controller  # type: ignore[assignment]
        # the status files move around the write, and the write is of the standby
        assert _boot_controller.pre_updated == (False, True)
        assert _installer.installed is not None
        _staged = tmp_path / "staging" / "3.0.0" / "image"
        assert _installer.installed["package"] == _staged
        # the installer records the version the payload carries...
        assert _installer.installed["version"] == "2.9.0"
        # ...and otaclient the one the request named
        assert _boot_controller.post_updated == ("3.0.0", None)
        # the rootfs image and the boot files, the manifest and the config
        assert _installer.blobs_seen == sorted(
            [
                "f" * 64,
                "c" * 64,
                *(
                    p.name
                    for p in (_served / RESOURCE_DIR).iterdir()
                    if p.name not in ("f" * 64, "c" * 64)
                ),
            ]
        )
        assert _boot_controller.rebooted is True
        assert not _staged.exists(), "what was staged is dead weight once written"
        assert not _boot_controller.failed

    def test_a_payload_the_installer_refuses_is_a_failed_update(
        self, tmp_path: Path, index_verified_from
    ):
        """The installer's verdict is reported as the failure to apply, the boot
        control records it, and what was downloaded stays for a retry."""
        _served = serve_an_image(tmp_path / "served", config(rootfs(), boot()))
        index_verified_from(_served)
        _updater = make_full_updater(
            tmp_path, installer=FakeInstaller(install_error=True), served=_served
        )

        with pytest.raises(ota_errors.ApplyOTAUpdateFailed, match="did not apply"):
            _updater.execute()

        _boot_controller: FakeBootController = _updater._boot_controller  # type: ignore[assignment]
        assert _boot_controller.failed is True
        assert _boot_controller.rebooted is False
        assert (
            tmp_path / "staging" / "3.0.0" / "image" / RESOURCE_DIR / ("f" * 64)
        ).is_file()

    def test_a_payload_of_data_images_alone_is_refused_before_downloading(
        self, tmp_path: Path, index_verified_from
    ):
        """Applying it is a trial boot of the running slot, which the boot control of
        this client does not do: refused as what it is, with nothing fetched."""
        _served = serve_an_image(
            tmp_path / "served",
            config(
                PartitionEntry(name="rootfs", action=PartitionAction.keep),
                PartitionEntry(name="boot", action=PartitionAction.keep),
                data_images=(data_image(),),
            ),
        )
        index_verified_from(_served)
        _installer = FakeInstaller()
        _updater = make_full_updater(tmp_path, installer=_installer, served=_served)

        with pytest.raises(ota_errors.OTAImageInvalid, match="writes no slot"):
            _updater.execute()

        assert _installer.installed is None
        _staged_blobs = tmp_path / "staging" / "3.0.0" / "image" / RESOURCE_DIR
        assert sorted(p.name for p in _staged_blobs.iterdir()) == sorted(
            p.name for p in (_served / RESOURCE_DIR).iterdir() if p.name != "d" * 64
        ), "the manifest and the config, not the data image"


class TestFinalize:
    """The last phase: this ECU does not reboot while another is still updating."""

    def test_it_waits_for_a_child_ecu_before_rebooting(
        self, tmp_path: Path, no_wait_before_reboot
    ):
        _child_updating = threading.Event()
        _child_updating.set()
        _updater = make_full_updater(tmp_path, child_in_update=_child_updating)
        _staging = tmp_path / "staging" / "3.0.0"
        _staging.mkdir(parents=True)

        _done = threading.Event()

        def _run():
            _updater._finalize(_staging)
            _done.set()

        threading.Thread(target=_run, daemon=True).start()
        assert not _done.wait(timeout=1)  # the child is still updating: no reboot
        assert _updater._boot_controller.rebooted is False  # type: ignore[attr-defined]

        _child_updating.clear()  # the child is done

        assert _done.wait(timeout=10)
        assert _updater._boot_controller.rebooted is True  # type: ignore[attr-defined]

    def test_what_was_staged_is_removed_once_it_is_written(
        self, tmp_path: Path, no_wait_before_reboot
    ):
        """Gigabytes on persistent storage, of use only to a retry that will not
        happen."""
        _updater = make_full_updater(tmp_path)
        _staging = tmp_path / "staging" / "3.0.0"
        (_staging / "blobs").mkdir(parents=True)
        (_staging / "blobs" / "big").write_bytes(b"x" * 1024)

        _updater._finalize(_staging)

        assert not _staging.exists()
        assert _updater._boot_controller.rebooted is True  # type: ignore[attr-defined]


def test_an_earlier_updates_staging_is_cleared_before_a_new_one(tmp_path: Path):
    """Each update stages under its own version; last time's is dead weight."""
    _updater = make_full_updater(tmp_path)
    _old = tmp_path / "staging" / "2.9.0"
    _old.mkdir(parents=True)
    _keep = tmp_path / "staging" / "3.0.0"
    _keep.mkdir(parents=True)

    _updater._prune_staging(keep=_keep)

    assert not _old.exists()
    assert _keep.exists()


class TestTheUpdateAgentTheImageShips:
    """The consumer that installs a bundle from the agent package is the installer,
    and otaclient is what fetches. An entry the index names and the blobs do not
    carry is an image the installer refuses after otaclient has downloaded
    gigabytes."""

    @staticmethod
    def agent_manifest(*bundles):
        from hashlib import sha256

        from ota_image_libs.v1.annotation_keys import (
            UPDATE_AGENT_ARCH,
            UPDATE_AGENT_TYPE,
            UPDATE_AGENT_VERSION,
        )
        from ota_image_libs.v1.media_types import UPDATE_AGENT_BUNDLE
        from ota_image_libs.v1.update_agent_package.schema import (
            EMPTY_JSON,
            EmptyConfigDescriptor,
            UpdateAgentBundleDescriptor,
            UpdateAgentPackageManifest,
        )

        return UpdateAgentPackageManifest(
            config=EmptyConfigDescriptor(
                size=len(EMPTY_JSON),
                digest=f"sha256:{sha256(EMPTY_JSON).hexdigest()}",
            ),
            layers=[
                UpdateAgentBundleDescriptor(
                    mediaType=UPDATE_AGENT_BUNDLE,
                    digest=f"sha256:{str(n) * 64}",
                    size=n,
                    annotations={
                        UPDATE_AGENT_TYPE: t,
                        UPDATE_AGENT_VERSION: v,
                        UPDATE_AGENT_ARCH: a,
                    },
                )
                for n, (t, v, a) in enumerate(bundles, start=1)
            ],
        )

    def test_every_bundle_is_fetched_beside_a_vendor_package(self, tmp_path: Path):
        """All of them, not the one otaclient would run: which type and architecture
        the installer wants is its business. They are megabytes beside gigabytes."""
        _updater = make_updater(tmp_path)
        _updater.image_config = config(
            PartitionEntry(
                name="rootfs", action=PartitionAction.write, performed_by="package"
            ),
            delivery=DeliveryMode.vendor_package,
        )
        _updater.update_agent_manifest = self.agent_manifest(
            ("tier4.ota.agent.v1", "1.3.0", "arm64"),
            ("tier4.otaclient.squashfs.v1", "3.14.0", "x86_64"),
        )

        _plan = _updater.plan()

        assert [_b.what for _b in _plan.blobs] == [
            "the vendor package",
            "the tier4.ota.agent.v1 agent 1.3.0",
            "the tier4.otaclient.squashfs.v1 agent 3.14.0",
        ]

    def test_they_are_fetched_beside_a_direct_payload_too(self, tmp_path: Path):
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs())
        _updater.update_agent_manifest = self.agent_manifest(
            ("tier4.ota.agent.v1", "1.3.0", "arm64")
        )

        assert any("agent 1.3.0" in _b.what for _b in _updater.plan().blobs)

    def test_an_image_without_one_plans_exactly_what_it_did_before(
        self, tmp_path: Path
    ):
        _updater = make_updater(tmp_path)
        _updater.image_config = config(
            PartitionEntry(
                name="rootfs", action=PartitionAction.write, performed_by="package"
            ),
            delivery=DeliveryMode.vendor_package,
        )
        assert [_b.what for _b in _updater.plan().blobs] == ["the vendor package"]
