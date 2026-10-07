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
"""Deciding what a partition-based payload costs this device, and fetching that.

The decision is the whole point of these tests: a delta applies to bytes named by
digest, the device is the only thing that knows whether it holds them, and the answer
has to be reached before a gigabyte is downloaded rather than after.
"""

from __future__ import annotations

import threading
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

from otaclient import errors as ota_errors
from otaclient.boot_control._dpi import DPIError
from otaclient.configs import BootloaderType
from otaclient.ota_core._partition_image import (
    STAGING_DIR,
    OTAUpdaterForPartitionImage,
    PartitionImageUpdater,
    staging_dir_for,
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
        self.resumed_from: List[int] = []
        self._served = served
        self._fail_with = fail_with

    def download(self, url, dst, *, digest=None, size=None, resume_from=0, **kwargs):
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        self.calls.append((url, dst))
        self.resumed_from.append(resume_from)
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


class FakeDPI:
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
        self.asked: list[tuple[int, Optional[str]]] = []
        self.data_on_device: dict[str, str] = {}

    def source_digest(self, *, size: int, data_image: Optional[str] = None) -> str:
        if self._digest_error:
            raise DPIError("cannot read the slot")
        self.asked.append((size, data_image))
        if data_image is not None:
            return self.data_on_device.get(data_image, "0" * 64)
        return self._on_device

    def install(self, **kwargs):
        if self._install_error:
            raise DPIError("the payload was refused")
        self.installed = kwargs
        return True


def make_updater(
    tmp_path: Path, dpi: Optional[FakeDPI] = None, served: Optional[Path] = None
):
    return PartitionImageUpdater(
        base_url="http://ota.example/image/",
        downloader=FakeDownloader(served),
        session_dir=tmp_path,
        image_id=ImageIdentifier("autoware", OTAReleaseKey.dev),
        dpi=dpi or FakeDPI(),
    )


def serve_an_image(root: Path, payload_config: PartitionImageConfig, *, ecu="autoware"):
    """An OTA image as it is published: index.json, index.jwt and the blobs.

    Built with the same models the updater parses, so a field that changes shape
    breaks these tests rather than a campaign.
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
    _index = ImageIndex(
        manifests=[_manifest_desc],
        annotations={"vnd.tier4.ota.ota-image-builder.version": "0.6.0"},
    )
    (root / IMAGE_INDEX_FNAME).write_text(_index.export_metafile())
    # Not checked here: the DPI verifies it again on the device, against its own
    # trust anchor, and that is the check the partition write depends on.
    (root / INDEX_JWT_FNAME).write_text("not-verified-by-otaclient")
    return root


class TestDownloadMetadata:
    def test_it_reads_the_payload_for_this_ecu(self, tmp_path: Path):
        _served = serve_an_image(
            tmp_path / "served", config(rootfs(delta=delta_against(ON_DEVICE)))
        )
        _updater = make_updater(tmp_path / "session", served=_served)

        _config = _updater.download_metadata()

        assert _config.image_version == "2.9.0"
        assert _config.delivery is DeliveryMode.direct
        assert [_p.name for _p in _config.partitions] == ["rootfs"]

    def test_the_metadata_lands_where_the_dpi_reads_it(self, tmp_path: Path):
        """index.json and index.jwt go in unchanged, beside the blobs: the DPI
        verifies the signature again on the device, so otaclient hands it the bytes
        it was given rather than anything it re-derived."""
        _served = serve_an_image(tmp_path / "served", config(rootfs()))
        _updater = make_updater(tmp_path / "session", served=_served)

        _updater.download_metadata()

        _image = tmp_path / "session" / "image"
        assert (_image / IMAGE_INDEX_FNAME).read_bytes() == (
            _served / IMAGE_INDEX_FNAME
        ).read_bytes()
        assert (_image / INDEX_JWT_FNAME).is_file()
        # the manifest and the config, by digest, and nothing else yet
        assert len(list((_image / RESOURCE_DIR).iterdir())) == 2

    def test_an_image_without_a_payload_for_this_ecu_is_refused(self, tmp_path: Path):
        _served = serve_an_image(
            tmp_path / "served", config(rootfs()), ecu="perception"
        )
        _updater = make_updater(tmp_path / "session", served=_served)

        with pytest.raises(ota_errors.MetadataJWTInvalid, match="no partition-based"):
            _updater.download_metadata()

    def test_metadata_then_plan_reads_end_to_end(self, tmp_path: Path):
        """The two halves together: what the image says, and what this device needs
        of it."""
        _served = serve_an_image(
            tmp_path / "served",
            config(rootfs(delta=delta_against(ON_DEVICE))),
        )
        _updater = make_updater(tmp_path / "session", served=_served)

        _updater.download_metadata()
        _plan = _updater.plan()

        assert [_b.what for _b in _plan.blobs] == ["the rootfs delta"]


class TestPlan:
    def test_a_delta_that_fits_is_what_gets_downloaded(self, tmp_path: Path):
        _updater = make_updater(tmp_path)
        _updater.image_config = config(
            rootfs(delta=delta_against(ON_DEVICE)),
            PartitionEntry(
                name="boot", action=PartitionAction.write, image=boot_files("c" * 64)
            ),
        )

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
        _updater.image_config = config(
            rootfs(),
            PartitionEntry(
                name="boot", action=PartitionAction.write, image=boot_files("c" * 64)
            ),
            data_images=(data_image(),),
        )
        _plan = _updater.plan()
        assert [_b.what for _b in _plan.blobs] == [
            "the rootfs image",
            "the boot image",
            "the ml_package data image",
        ]

    def test_a_data_image_delta_is_planned_when_the_device_holds_its_source(
        self, tmp_path: Path
    ):
        _dpi = FakeDPI()
        _dpi.data_on_device["ml_package"] = ON_DEVICE
        _updater = make_updater(tmp_path, dpi=_dpi)
        _updater.image_config = config(
            PartitionEntry(name="rootfs", action=PartitionAction.keep),
            PartitionEntry(name="boot", action=PartitionAction.keep),
            data_images=(data_image(delta=delta_against(ON_DEVICE)),),
        )
        _plan = _updater.plan()
        assert [_b.what for _b in _plan.blobs] == ["the ml_package data image delta"]
        # asked of the data image's file, not of the rootfs slot
        assert _dpi.asked[-1][1] == "ml_package"

    def test_a_data_image_delta_that_does_not_fit_is_refused(self, tmp_path: Path):
        _dpi = FakeDPI()
        _dpi.data_on_device["ml_package"] = "9" * 64
        _updater = make_updater(tmp_path, dpi=_dpi)
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
        """otaclient fetches the package; the platform's own updater applies it —
        NVIDIA's OTA tools on L4T, DRIVE Update on AD1. There is nothing to choose
        between and no partition to size against, so the plan is that one blob."""
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
    def test_only_the_planned_blobs_are_fetched_and_where_the_dpi_reads_them(
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
        assert _urls == [f"http://ota.example/image/blobs/sha256/{'d' * 64}"]

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

    def test_a_half_downloaded_blob_is_continued_from_where_it_stopped(
        self, tmp_path: Path
    ):
        """A rootfs image is gigabytes and a link that drops at 90% should not cost
        all of it. What is on disk is offered to the downloader as a byte count; the
        digest is still computed over the whole file, so the resumed part is trusted
        no further than a fresh one."""
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))
        _plan = _updater.plan()
        _delta = _plan.blobs[0]
        _part = tmp_path / "image" / RESOURCE_DIR / f"{_delta.digest_hex}.part"
        _part.parent.mkdir(parents=True)
        _part.write_bytes(b"x" * (_delta.size // 2))

        _updater.download_payload(_plan)

        assert _updater._downloader.resumed_from == [_delta.size // 2]
        _, _dst = _updater._downloader.calls[0]
        assert _dst == _part, "the bytes go on arriving in the same file"
        # and only a whole, verified blob carries the digest as its name
        assert (tmp_path / "image" / RESOURCE_DIR / _delta.digest_hex).is_file()
        assert not _part.exists()

    def test_a_part_longer_than_the_blob_is_not_a_prefix_of_it(self, tmp_path: Path):
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))
        _plan = _updater.plan()
        _delta = _plan.blobs[0]
        _part = tmp_path / "image" / RESOURCE_DIR / f"{_delta.digest_hex}.part"
        _part.parent.mkdir(parents=True)
        _part.write_bytes(b"x" * (_delta.size + 1))

        _updater.download_payload(_plan)

        assert _updater._downloader.resumed_from == [0]

    def test_bytes_that_do_not_hash_to_the_digest_are_not_resumed_from(
        self, tmp_path: Path
    ):
        """A whole file that hashes to something else is not this blob in part either,
        and the next attempt must not build on it."""
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

    def test_an_interrupted_download_leaves_its_bytes_for_the_next_attempt(
        self, tmp_path: Path
    ):
        """The campaign is resent and pays only for what did not arrive."""
        _updater = make_updater(tmp_path)
        _updater._downloader = FakeDownloader(fail_with=ConnectionError("link dropped"))
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))
        _plan = _updater.plan()
        _delta = _plan.blobs[0]
        _part = tmp_path / "image" / RESOURCE_DIR / f"{_delta.digest_hex}.part"
        _part.parent.mkdir(parents=True)
        _arrived = _delta.size // 2
        _part.write_bytes(b"x" * _arrived)

        with pytest.raises(ConnectionError):
            _updater.download_payload(_plan)

        assert _part.is_file() and _part.stat().st_size == _arrived

    def test_a_device_without_room_is_told_before_the_download_starts(
        self, tmp_path: Path, monkeypatch
    ):
        """Partway through is where it would otherwise be found — after minutes of
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

    def test_apply_hands_the_unpacked_image_to_the_dpi(self, tmp_path: Path):
        _dpi = FakeDPI()
        _updater = make_updater(tmp_path, _dpi)

        assert _updater.apply(version="2.9.0", name="T4-ROOTFS") is True

        assert _dpi.installed is not None
        assert _dpi.installed["package"] == tmp_path / "image"
        assert _dpi.installed["version"] == "2.9.0"
        assert _dpi.installed["name"] == "T4-ROOTFS"
        assert _dpi.installed["rollback"] is False

    def test_a_refused_payload_is_a_failed_update(self, tmp_path: Path):
        _updater = make_updater(tmp_path, FakeDPI(install_error=True))
        with pytest.raises(ota_errors.ApplyOTAUpdateFailed):
            _updater.apply(version="2.9.0", name="T4-ROOTFS")


class FakeBootController:
    """What _finalize asks of boot control."""

    def __init__(self) -> None:
        self.rebooted = False

    def finalizing_update(self, *, chroot=None) -> None:
        self.rebooted = True


def make_full_updater(tmp_path: Path, *, child_in_update: threading.Event):
    """OTAUpdaterForPartitionImage with otaclient's own machinery, minus the parts a
    single ECU cannot have: the shared-memory metrics reader and the abort handler."""
    from otaclient._types import MultipleECUStatusFlags
    from otaclient.metrics import OTAMetricsData

    _flags = MultipleECUStatusFlags(
        any_child_ecu_in_update=child_in_update,
        any_requires_network=threading.Event(),
        all_success=threading.Event(),
    )
    return OTAUpdaterForPartitionImage(
        boot_controller=FakeBootController(),
        abort_handler=SimpleNamespace(state=None),
        image_identifier=ImageIdentifier("autoware", OTAReleaseKey.dev),
        dpi=FakeDPI(),
        staging_dir=tmp_path / "staging",
        version="3.0.0",
        raw_url_base="http://ota.example/image/",
        session_wd=tmp_path / "session",
        downloader_pool=SimpleNamespace(
            get_instance=lambda: FakeDownloader(), release_instance=lambda: None
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


class TestFinalize:
    """The last phase: this ECU does not reboot while another is still updating."""

    def test_it_waits_for_a_child_ecu_before_rebooting(
        self, tmp_path: Path, monkeypatch
    ):
        import otaclient.ota_core._updater_base as _base

        monkeypatch.setattr(_base, "WAIT_BEFORE_REBOOT", 0)
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
        assert _updater._boot_controller.rebooted is False

        _child_updating.clear()  # the child is done

        assert _done.wait(timeout=10)
        assert _updater._boot_controller.rebooted is True

    def test_what_was_staged_is_removed_once_it_is_written(
        self, tmp_path: Path, monkeypatch
    ):
        """Gigabytes on optdata, of use only to a retry that will not happen."""
        import otaclient.ota_core._updater_base as _base

        monkeypatch.setattr(_base, "WAIT_BEFORE_REBOOT", 0)
        _updater = make_full_updater(tmp_path, child_in_update=threading.Event())
        _staging = tmp_path / "staging" / "3.0.0"
        (_staging / "blobs").mkdir(parents=True)
        (_staging / "blobs" / "big").write_bytes(b"x" * 1024)

        _updater._finalize(_staging)

        assert not _staging.exists()
        assert _updater._boot_controller.rebooted is True


def test_an_earlier_updates_staging_is_cleared_before_a_new_one(tmp_path: Path):
    """Each update stages under its own version; last time's is dead weight."""
    _updater = make_full_updater(tmp_path, child_in_update=threading.Event())
    _old = tmp_path / "staging" / "2.9.0"
    _old.mkdir(parents=True)
    _keep = tmp_path / "staging" / "3.0.0"
    _keep.mkdir(parents=True)

    _updater._prune_staging(keep=_keep)

    assert not _old.exists()
    assert _keep.exists()


class TestWhereThePayloadIsStaged:
    """Gigabytes have to land somewhere that survives until the DPI has consumed
    them, and "outside the slots" is not available on every device side."""

    def test_the_grub_layout_stages_on_optdata(self):
        assert staging_dir_for(BootloaderType.GRUB_VERITY).startswith("/opt/data")

    def test_a_jetson_stages_inside_the_running_slot(self):
        """L4T's only partition outside the slots is UDA, a few hundred megabytes --
        room for a record, not for a payload. The staged bytes are consumed by the
        write that happens before the reboot, so the running slot is enough."""
        assert staging_dir_for(BootloaderType.JETSON_DPI).startswith("/var/tmp")

    def test_an_unknown_one_falls_back_to_the_layout_this_was_written_for(self):
        assert staging_dir_for("something-else") == STAGING_DIR  # type: ignore[arg-type]


class TestTheUpdateAgentTheImageShips:
    """The consumer that installs a bundle from the agent package is the DPI, and
    otaclient is what fetches. An entry the index names and the blobs do not carry is
    an image the DPI refuses after otaclient has downloaded gigabytes."""

    @staticmethod
    def agent_manifest(*bundles):
        from ota_image_libs.v1.annotation_keys import (
            UPDATE_AGENT_ARCH,
            UPDATE_AGENT_TYPE,
            UPDATE_AGENT_VERSION,
        )
        from ota_image_libs.v1.media_types import UPDATE_AGENT_BUNDLE
        from ota_image_libs.v1.update_agent_package.schema import (
            UpdateAgentBundleDescriptor,
            UpdateAgentPackageManifest,
        )

        return UpdateAgentPackageManifest(
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
            ]
        )

    def test_every_bundle_is_fetched_beside_a_vendor_package(self, tmp_path: Path):
        """All of them, not the one otaclient would run: which type and architecture
        the DPI wants is its business. They are megabytes beside gigabytes."""
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
