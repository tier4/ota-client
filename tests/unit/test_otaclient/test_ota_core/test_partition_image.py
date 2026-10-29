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

The downloads go through the real `DownloadHelperForOTAImageV1` and a real
`DownloaderPool`, with `Downloader.download` replaced by one that serves a directory
on disk: what the updater yields and what it does afterwards is what is under test,
the retry pool's own behaviour is tested with it.
"""

from __future__ import annotations

import errno
import shutil
import threading
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

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
    FirmwareEntry,
    FirmwarePackageDescriptor,
    PartitionAction,
    PartitionDeltaDescriptor,
    PartitionEntry,
    PartitionImageBlobDescriptor,
    PartitionImageConfig,
    PartitionImageManifest,
    VendorPackageDescriptor,
)
from pytest_mock import MockerFixture
from requests import HTTPError, Response

from ota_metadata.utils.cert_store import CAStoreMap
from otaclient import errors as ota_errors
from otaclient._status_monitor import (
    OTAUpdatePhaseChangeReport,
    SetUpdateMetaReport,
    UpdateProgressReport,
)
from otaclient._types import AbortState, MultipleECUStatusFlags, UpdatePhase
from otaclient._utils import safe_dir_name
from otaclient.boot_control.protocol import PartitionInstallError
from otaclient.metrics import OTAMetricsData
from otaclient.ota_core import _partition_image as _mod
from otaclient.ota_core import _updater_base as _base
from otaclient.ota_core._common import download_exception_handler
from otaclient.ota_core._download_resources import DownloadHelperForOTAImageV1
from otaclient.ota_core._partition_image import (
    STAGING_SUBDIR,
    OTAUpdaterForPartitionImage,
    PartitionImageUpdater,
    _ensure_the_staging_root_is_ours,
)
from otaclient_common.downloader import Downloader, DownloaderPool, DownloadResult

PARTITION_BLOB_MEDIA = PARTITION_IMAGE_BLOB
PARTITION_DELTA_MEDIA = PARTITION_IMAGE_DELTA
BOOT_FILES_MEDIA = (
    "application/vnd.tier4.ota.partition-based-ota-image.boot-files.v1.tar"
)
VENDOR_PACKAGE_MEDIA = (
    "application/vnd.tier4.ota.partition-based-ota-image.vendor-package.v1"
)
FIRMWARE_PACKAGE_MEDIA = (
    "application/vnd.tier4.ota.partition-based-ota-image.firmware-package.v1"
)

BASE_URL = "http://ota.example/image/"
THIS_ECU = ImageIdentifier("autoware", OTAReleaseKey.dev)

ON_DEVICE = "a" * 64
"""The digest of what the committed slot holds, in these tests."""
SOMETHING_ELSE = "b" * 64

DELTA_BYTES = b"x" * 100
DELTA_DIGEST = sha256(DELTA_BYTES).hexdigest()
"""The one blob whose bytes these tests have: what a delta descriptor names."""


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
        digest=f"sha256:{DELTA_DIGEST}",
        size=size,
        annotations={
            "vnd.tier4.ota.partition-image.delta.algorithm": DELTA_ALGORITHM_BLOCK_DIFF,
            "vnd.tier4.ota.partition-image.delta.source-digest": f"sha256:{source}",
            "vnd.tier4.ota.partition-image.delta.source-size": 4096,
        },
    )


def firmware(name: str = "T4-L4T-BSP") -> FirmwareEntry:
    """The BSP capsule an L4T release carries beside its rootfs."""
    return FirmwareEntry(
        name=name,
        version="39.2.1",
        format="nvidia-l4t.uefi-capsule.v1",
        package=FirmwarePackageDescriptor(
            mediaType=FIRMWARE_PACKAGE_MEDIA,
            digest=f"sha256:{'9' * 64}",
            size=18341959,
            annotations={
                "vnd.tier4.ota.partition-image.firmware.format": "nvidia-l4t.uefi-capsule.v1"
            },
        ),
    )


def config(
    *partitions: PartitionEntry,
    delivery: DeliveryMode = DeliveryMode.direct,
    data_images: tuple[DataImageEntry, ...] = (),
    firmware: FirmwareEntry | None = None,
):
    """A payload config, built directly: what the updater reads, without the
    downloading that puts it there."""
    _kwargs = dict(
        architecture="x86_64",
        image_version="2.9.0",
        delivery=delivery,
        partitions=list(partitions),
        data_images=list(data_images),
        firmware=firmware,
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
    *, delta: PartitionDeltaDescriptor | None = None, name: str = "ml_package"
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


def rootfs(*, delta: PartitionDeltaDescriptor | None = None) -> PartitionEntry:
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


def vendor_written(name: str) -> PartitionEntry:
    return PartitionEntry(
        name=name, action=PartitionAction.write, performed_by="package"
    )


#
# ------ the downloads, served from a directory ------ #
#


class FakeServer:
    """What `Downloader.download` is replaced with: serves a directory as the OTA
    image's URL base, records every request, and can fail them.

    A ranged request (`resume_from`) is honoured as a server that supports it does:
    the file goes on from there. With `drops`, the link drops that many times, each
    time after `per_drop` bytes of the attempt arrived. What arrives is counted on
    the downloader as the real one counts it, as traffic on the wire.
    """

    def __init__(
        self, served: Path | None = None, fail_with: Exception | None = None
    ) -> None:
        self.calls: list[tuple[str, Path]] = []
        self.resumed_from: list[int] = []
        self.drops, self.per_drop = 0, 10
        self._served = served
        self._fail_with = fail_with

    def download(
        self, _downloader, *, url, dst, size=None, resume_from=0, **_
    ) -> DownloadResult:
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        self.calls.append((url, dst))
        self.resumed_from.append(resume_from)
        if self._fail_with is not None:
            raise self._fail_with
        if self._served is not None:
            _src = self._served / url.split("/image/", 1)[1]
            if not _src.is_file():
                raise FileNotFoundError(f"404: {url}")
            _data = _src.read_bytes()
        elif size == len(DELTA_BYTES):
            _data = DELTA_BYTES
        else:
            _data = b"\0" * (size or 1)

        _rest = _data[resume_from:]
        _dropped = self.drops > 0
        if _dropped:
            self.drops -= 1
            _rest = _rest[: self.per_drop]
        with open(dst, "r+b" if resume_from else "wb") as _f:
            _f.seek(resume_from)
            _f.write(_rest)
        _downloader._downloaded_bytes += len(_rest)
        if _dropped:
            raise ConnectionError("link dropped")
        return DownloadResult(download_size=len(_rest), traffic_on_wire=len(_rest))


def http_error(status: int) -> HTTPError:
    _response = Response()
    _response.status_code = status
    return HTTPError(f"{status} for the test", response=_response)


@pytest.fixture
def pool():
    _pool = DownloaderPool(instance_num=2, hash_func=sha256)
    try:
        yield _pool
    finally:
        _pool.shutdown()


@pytest.fixture
def server(mocker: MockerFixture):
    """`Downloader.download` of every instance of the pool goes to one FakeServer,
    configured by the test through `serve(dir)` / `fail(exc)`."""
    _server = FakeServer()
    mocker.patch.object(
        Downloader, "download", autospec=True, side_effect=_server.download
    )

    def serve(served: Path | None = None, *, fail_with: Exception | None = None):
        _server._served, _server._fail_with = served, fail_with
        return _server

    return serve


def drive(generator, *, pool: DownloaderPool, condition: threading.Condition):
    """Run the updater's download generator through the download helper, as
    `execute()` does, with the helper's own error mapping."""
    _helper = DownloadHelperForOTAImageV1(
        downloader_pool=pool, max_concurrent=2, download_inactive_timeout=5
    )
    for _fut in _helper.download_meta_files(generator, condition=condition):
        download_exception_handler(_fut)


class FakeInstaller:
    """The device side: what it holds, and what it was handed."""

    def __init__(
        self,
        *,
        on_device: str = ON_DEVICE,
        digest_error: bool = False,
        install_error: Exception | None = None,
    ) -> None:
        self._on_device = on_device
        self._digest_error = digest_error
        self._install_error = install_error
        self.installed: dict | None = None
        self.blobs_seen: list[str] = []
        self.asked: list[tuple[int, str | None]] = []
        self.data_on_device: dict[str, str] = {}
        self.progress: tuple[int, ...] = (0, 50, 100)

    def source_digest(self, *, size: int, data_image: str | None = None) -> str:
        if self._digest_error:
            raise PartitionInstallError("cannot read the slot")
        self.asked.append((size, data_image))
        if data_image is not None:
            return self.data_on_device.get(data_image, "0" * 64)
        return self._on_device

    def install(self, *, package, on_progress=None, **kwargs) -> None:
        if self._install_error is not None:
            raise self._install_error
        self.installed = dict(package=Path(package), **kwargs)
        self.blobs_seen = sorted(
            p.name for p in (Path(package) / RESOURCE_DIR).iterdir()
        )
        if on_progress is not None:
            for _percent in self.progress:
                on_progress(_percent)


def make_updater(tmp_path: Path, installer: FakeInstaller | None = None):
    return PartitionImageUpdater(
        base_url=BASE_URL,
        session_dir=tmp_path,
        image_id=THIS_ECU,
        installer=installer or FakeInstaller(),
    )


def serve_an_image(root: Path, payload_config: PartitionImageConfig, *, ecu="autoware"):
    """An OTA image as it is published: index.json, index.jwt and the blobs.

    Built with the same models the updater parses, so a field that changes shape
    breaks these tests rather than a campaign. The payload's blobs are there under
    their digests, with whatever bytes: the fake server checks nothing.

    Returns the root and the digests of the manifest and the config it wrote.
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
    return root, (_manifest_desc.digest.digest_hex, _config_desc.digest.digest_hex)


def read_metadata(updater: PartitionImageUpdater, served: Path, *, pool):
    """What `OTAUpdaterForPartitionImage` does once the index is verified: hand the
    parsed index and the two index files to the updater, through the helper."""
    _index = ImageIndex.parse_metafile((served / IMAGE_INDEX_FNAME).read_text())
    _condition = threading.Condition()
    drive(
        updater.download_metadata(
            _index,
            index_json=served / IMAGE_INDEX_FNAME,
            index_jwt=served / INDEX_JWT_FNAME,
            condition=_condition,
        ),
        pool=pool,
        condition=_condition,
    )
    assert updater.image_config is not None
    return updater.image_config


def download(
    updater: PartitionImageUpdater,
    plan,
    *,
    pool,
    on_blob_start=None,
    on_blob_done=None,
):
    _condition = threading.Condition()
    drive(
        updater.download_payload(
            plan,
            condition=_condition,
            on_blob_start=on_blob_start,
            on_blob_done=on_blob_done,
        ),
        pool=pool,
        condition=_condition,
    )
    return updater.image_dir


class TestDownloadMetadata:
    def test_it_reads_the_payload_for_this_ecu(self, tmp_path: Path, pool, server):
        _served, _ = serve_an_image(
            tmp_path / "served", config(rootfs(delta=delta_against(ON_DEVICE)))
        )
        server(_served)
        _updater = make_updater(tmp_path / "session")

        _config = read_metadata(_updater, _served, pool=pool)

        assert _config.image_version == "2.9.0"
        assert _config.delivery is DeliveryMode.direct
        assert [_p.name for _p in _config.partitions] == ["rootfs"]

    def test_the_metadata_lands_where_the_installer_reads_it(
        self, tmp_path: Path, pool, server
    ):
        """index.json and index.jwt go in unchanged, beside the blobs: the installer
        verifies the signature again on the device, so otaclient hands it the bytes
        it was given rather than anything it re-derived."""
        _served, (_manifest, _config) = serve_an_image(
            tmp_path / "served", config(rootfs())
        )
        server(_served)
        _updater = make_updater(tmp_path / "session")

        read_metadata(_updater, _served, pool=pool)

        _image = tmp_path / "session" / "image"
        assert (_image / IMAGE_INDEX_FNAME).read_bytes() == (
            _served / IMAGE_INDEX_FNAME
        ).read_bytes()
        assert (_image / INDEX_JWT_FNAME).read_bytes() == (
            _served / INDEX_JWT_FNAME
        ).read_bytes()
        # the manifest and the config, by digest, and nothing else yet
        assert sorted(p.name for p in (_image / RESOURCE_DIR).iterdir()) == sorted(
            [_manifest, _config]
        )

    def test_an_image_without_a_payload_for_this_ecu_is_refused(
        self, tmp_path: Path, pool, server
    ):
        _served, _ = serve_an_image(
            tmp_path / "served", config(rootfs()), ecu="perception"
        )
        server(_served)
        _updater = make_updater(tmp_path / "session")

        with pytest.raises(ota_errors.OTAImageInvalid, match="no partition-based"):
            read_metadata(_updater, _served, pool=pool)

    def test_a_manifest_the_server_does_not_have_is_an_image_not_found(
        self, tmp_path: Path, pool, server
    ):
        """The helper's error mapping applies to these downloads as to the metafiles
        of a file-based update: a 404 is E404, not an unspecific failure."""
        _served, _ = serve_an_image(tmp_path / "served", config(rootfs()))
        server(_served, fail_with=http_error(404))
        _updater = make_updater(tmp_path / "session")

        with pytest.raises(ota_errors.OTAImageNotFound):
            read_metadata(_updater, _served, pool=pool)

    def test_metadata_then_plan_reads_end_to_end(self, tmp_path: Path, pool, server):
        """The two halves together: what the image says, and what this device needs
        of it."""
        _served, _ = serve_an_image(
            tmp_path / "served",
            config(rootfs(delta=delta_against(ON_DEVICE))),
        )
        server(_served)
        _updater = make_updater(tmp_path / "session")

        read_metadata(_updater, _served, pool=pool)
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

        with pytest.raises(ota_errors.ApplyOTAUpdateFailed, match="does not hold"):
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
            "the ml_package data image image",
        ]

    def test_a_data_image_delta_is_planned_when_the_device_holds_its_source(
        self, tmp_path: Path
    ):
        _installer = FakeInstaller()
        _installer.data_on_device["ml_package"] = ON_DEVICE
        _updater = make_updater(tmp_path, installer=_installer)
        _updater.image_config = config(
            rootfs(),
            data_images=(data_image(delta=delta_against(ON_DEVICE)),),
        )
        _plan = _updater.plan()
        assert [_b.what for _b in _plan.blobs] == [
            "the rootfs image",
            "the ml_package data image delta",
        ]
        # asked of the data image's file, not of the rootfs slot
        assert _installer.asked[-1][1] == "ml_package"

    def test_a_data_image_delta_that_does_not_fit_is_refused(self, tmp_path: Path):
        _installer = FakeInstaller()
        _installer.data_on_device["ml_package"] = "9" * 64
        _updater = make_updater(tmp_path, installer=_installer)
        _updater.image_config = config(
            rootfs(),
            data_images=(data_image(delta=delta_against(ON_DEVICE)),),
        )
        with pytest.raises(
            ota_errors.ApplyOTAUpdateFailed, match="ml_package data image delta"
        ):
            _updater.plan()

    def test_the_firmware_package_rides_with_the_rootfs(self, tmp_path: Path):
        """An L4T release carries the BSP capsule beside its rootfs; the installer
        stages it and the same trial boot judges it, so it is fetched like the rest."""
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(), boot(), firmware=firmware())

        _plan = _updater.plan()

        assert [_b.what for _b in _plan.blobs] == [
            "the rootfs image",
            "the boot image",
            "the T4-L4T-BSP firmware package 39.2.1",
        ]

    def test_a_vendor_package_is_fetched_whole_with_what_rides_beside_it(
        self, tmp_path: Path
    ):
        """otaclient fetches the package; the platform's own updater applies it.
        Data images and firmware are the installer's under either delivery, so they
        are fetched beside the package."""
        _updater = make_updater(tmp_path)
        _updater.image_config = config(
            vendor_written("rootfs"),
            vendor_written("boot"),
            delivery=DeliveryMode.vendor_package,
            data_images=(data_image(),),
            firmware=firmware(),
        )

        _plan = _updater.plan()

        assert [_b.what for _b in _plan.blobs] == [
            "the ml_package data image image",
            "the T4-L4T-BSP firmware package 39.2.1",
            "the vendor package",
        ]

    @pytest.mark.parametrize(
        "payload",
        [
            config(rootfs(), boot(), data_images=(data_image(),), firmware=firmware()),
            config(
                rootfs(delta=delta_against(ON_DEVICE)),
                boot(),
                data_images=(data_image(delta=delta_against(ON_DEVICE)),),
            ),
            config(
                vendor_written("rootfs"),
                delivery=DeliveryMode.vendor_package,
                data_images=(data_image(),),
                firmware=firmware(),
            ),
        ],
        ids=["direct", "direct-with-deltas", "vendor-package"],
    )
    def test_the_plan_is_what_the_payload_ships(self, tmp_path: Path, payload):
        """The config lists the blobs a payload ships; the plan fetches exactly
        those when every delta fits, whatever the delivery."""
        _installer = FakeInstaller()
        _installer.data_on_device["ml_package"] = ON_DEVICE
        _updater = make_updater(tmp_path, installer=_installer)
        _updater.image_config = payload

        _planned = {_b.descriptor.digest.digest_hex for _b in _updater.plan().blobs}

        assert _planned == {_d.digest.digest_hex for _d in payload.payload_descriptors}

    def test_planning_before_reading_the_metadata_is_a_bug(self, tmp_path: Path):
        with pytest.raises(ValueError):
            make_updater(tmp_path).plan()


class TestDownloadAndApply:
    def test_only_the_planned_blobs_are_fetched_and_where_the_installer_reads_them(
        self, tmp_path: Path, pool, server
    ):
        """The unpacked image holds exactly what this device needs."""
        _server = server()
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))

        _image_dir = download(_updater, _updater.plan(), pool=pool)

        assert _image_dir == tmp_path / "image"
        _fetched = sorted(p.name for p in (_image_dir / "blobs/sha256").iterdir())
        assert _fetched == [DELTA_DIGEST]  # the delta, not the image beside it
        assert [url for url, _ in _server.calls] == [
            f"{BASE_URL}blobs/sha256/{DELTA_DIGEST}"
        ]

    def test_a_blob_left_by_an_earlier_attempt_is_reused(
        self, tmp_path: Path, pool, server
    ):
        """An interrupted update should not pay for its downloads twice."""
        _server = server()
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))
        _plan = _updater.plan()
        _dst = tmp_path / "image" / RESOURCE_DIR / DELTA_DIGEST
        _dst.parent.mkdir(parents=True)
        _dst.write_bytes(DELTA_BYTES)  # the bytes that hash to the digest
        _seen: list[tuple[str, str, bool | None]] = []

        download(
            _updater,
            _plan,
            pool=pool,
            on_blob_start=lambda _b, _here: _seen.append(("start", _b.what, None)),
            on_blob_done=lambda _b, _downloaded: _seen.append(
                ("done", _b.what, _downloaded)
            ),
        )

        assert _server.calls == []
        assert _seen == [
            ("start", "the rootfs delta", None),
            ("done", "the rootfs delta", False),
        ]

    def test_a_blob_of_the_right_length_but_the_wrong_bytes_is_downloaded_again(
        self, tmp_path: Path, pool, server
    ):
        """A truncated write or a bad disk leaves exactly that, and the next thing to
        read it writes it onto a partition."""
        _server = server()
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))
        _plan = _updater.plan()
        _dst = tmp_path / "image" / RESOURCE_DIR / DELTA_DIGEST
        _dst.parent.mkdir(parents=True)
        _dst.write_bytes(b"y" * 100)  # the right length, other bytes

        download(_updater, _plan, pool=pool)

        assert len(_server.calls) == 1
        assert _dst.read_bytes() == DELTA_BYTES

    def test_what_an_interrupted_attempt_left_is_told_before_the_blob_is_fetched(
        self, tmp_path: Path, pool, server
    ):
        """The part is continued, so the progress starts from it; a part that is
        not a prefix (as long as the blob or longer) is not."""
        server()
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))
        _plan = _updater.plan()
        _blobs = tmp_path / "image" / RESOURCE_DIR
        _blobs.mkdir(parents=True)
        (_blobs / (DELTA_DIGEST + ".part")).write_bytes(DELTA_BYTES[:40])
        _told: list[int] = []

        download(
            _updater,
            _plan,
            pool=pool,
            on_blob_start=lambda _b, _here: _told.append(_here),
        )
        assert _told == [40]

        (_blobs / DELTA_DIGEST).unlink()
        (_blobs / (DELTA_DIGEST + ".part")).write_bytes(DELTA_BYTES + b"more")
        _told.clear()
        download(
            _updater,
            _plan,
            pool=pool,
            on_blob_start=lambda _b, _here: _told.append(_here),
        )
        assert _told == [0]

    def test_a_half_downloaded_blob_is_continued_from_where_it_stopped(
        self, tmp_path: Path, pool, server
    ):
        """A rootfs image is gigabytes and a link that drops at 90% should not cost
        all of it. What is on disk is offered to the downloader as a byte count; the
        digest is still computed over the whole file, so the resumed part is trusted
        no further than a fresh one, and only the whole, verified file carries the
        digest as its name."""
        _server = server()
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))
        _plan = _updater.plan()
        _part = tmp_path / "image" / RESOURCE_DIR / f"{DELTA_DIGEST}.part"
        _part.parent.mkdir(parents=True)
        _part.write_bytes(DELTA_BYTES[:50])

        download(_updater, _plan, pool=pool)

        assert [_dst for _, _dst in _server.calls] == [_part], (
            "the bytes go on arriving in the same file"
        )
        assert _server.resumed_from == [50]
        _blob = tmp_path / "image" / RESOURCE_DIR / DELTA_DIGEST
        assert _blob.read_bytes() == DELTA_BYTES
        assert not _part.exists()

    def test_a_part_longer_than_the_blob_is_not_a_prefix_of_it(
        self, tmp_path: Path, pool, server
    ):
        _server = server()
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))
        _plan = _updater.plan()
        _part = tmp_path / "image" / RESOURCE_DIR / f"{DELTA_DIGEST}.part"
        _part.parent.mkdir(parents=True)
        _part.write_bytes(b"x" * (len(DELTA_BYTES) + 1))

        download(_updater, _plan, pool=pool)

        assert _server.resumed_from == [0]
        _blob = tmp_path / "image" / RESOURCE_DIR / DELTA_DIGEST
        assert _blob.read_bytes() == DELTA_BYTES

    def test_a_link_that_drops_and_comes_back_is_continued_in_the_same_update(
        self, tmp_path: Path, pool, server
    ):
        """Outdoors a link drops every few minutes. The helper retries the download,
        and each attempt goes on from what the one before left, so the update
        finishes instead of paying for the blob again at every drop."""
        _server = server()
        _server.drops, _server.per_drop = 3, 10
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))

        download(_updater, _updater.plan(), pool=pool)

        assert _server.resumed_from == [0, 10, 20, 30]
        _blob = tmp_path / "image" / RESOURCE_DIR / DELTA_DIGEST
        assert _blob.read_bytes() == DELTA_BYTES
        assert not _blob.with_name(_blob.name + ".part").exists()

    def test_a_download_the_server_refuses_keeps_the_helpers_error_code(
        self, tmp_path: Path, pool, server
    ):
        """A cookie that expired during a gigabyte is E401 here as on the file-based
        path, not an unspecific failure to apply."""
        server(fail_with=http_error(403))
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))

        with pytest.raises(ota_errors.UpdateRequestCookieInvalid):
            download(_updater, _updater.plan(), pool=pool)

        assert not (tmp_path / "image" / RESOURCE_DIR / DELTA_DIGEST).exists()

    def test_a_disk_that_fills_during_the_download_is_reported_as_such(
        self, tmp_path: Path, pool, server
    ):
        """The check before the download passed and the disk filled anyway: still a
        space problem (E507), and still one that clearing space and retrying fixes."""
        server(fail_with=OSError(errno.ENOSPC, "No space left on device"))
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))

        with pytest.raises(ota_errors.StandbySlotInsufficientSpace):
            download(_updater, _updater.plan(), pool=pool)

    def test_a_device_without_room_is_told_before_the_download_starts(
        self, tmp_path: Path, pool, server, monkeypatch
    ):
        """Partway through is where it would otherwise be found: after minutes of
        downloading, and reported as a failure to apply rather than as a device with
        no room."""
        _server = server()
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))
        _plan = _updater.plan()
        monkeypatch.setattr(
            _mod.shutil,
            "disk_usage",
            lambda _p: SimpleNamespace(free=_plan.total_size - 1),
        )

        with pytest.raises(ota_errors.StandbySlotInsufficientSpace, match="free"):
            download(_updater, _plan, pool=pool)

        assert _server.calls == []

    def test_what_is_already_staged_does_not_count_against_the_room_needed(
        self, tmp_path: Path, pool, server, monkeypatch
    ):
        """A retry has most of the payload on disk already; requiring room for all of
        it again would refuse an update that fits. A file of another length is not
        counted: it is downloaded again."""
        server()
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)), boot())
        _plan = _updater.plan()
        _delta, _boot = _plan.blobs
        _blob_dir = tmp_path / "image" / RESOURCE_DIR
        _blob_dir.mkdir(parents=True)
        (_blob_dir / _delta.digest_hex).write_bytes(DELTA_BYTES)
        (_blob_dir / _boot.digest_hex).write_bytes(b"short")  # not the boot files
        monkeypatch.setattr(
            _mod.shutil, "disk_usage", lambda _p: SimpleNamespace(free=_boot.size)
        )

        download(_updater, _plan, pool=pool)  # room for the boot files, which is enough

    def test_what_an_interrupted_download_left_counts_toward_the_room(
        self, tmp_path: Path, pool, server, monkeypatch
    ):
        """It is continued, not downloaded again, so only the rest of it needs room."""
        server()
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))
        _plan = _updater.plan()
        _part = tmp_path / "image" / RESOURCE_DIR / f"{DELTA_DIGEST}.part"
        _part.parent.mkdir(parents=True)
        _part.write_bytes(DELTA_BYTES[:60])
        monkeypatch.setattr(
            _mod.shutil,
            "disk_usage",
            lambda _p: SimpleNamespace(free=len(DELTA_BYTES) - 60),
        )

        download(_updater, _plan, pool=pool)  # room for the rest, which is enough

    def test_a_part_no_shorter_than_the_blob_does_not_count_toward_the_room(
        self, tmp_path: Path, pool, server, monkeypatch
    ):
        """It is not a prefix of the blob: it is downloaded again from the start, and
        all of it needs room."""
        server()
        _updater = make_updater(tmp_path)
        _updater.image_config = config(rootfs(delta=delta_against(ON_DEVICE)))
        _plan = _updater.plan()
        _part = tmp_path / "image" / RESOURCE_DIR / f"{DELTA_DIGEST}.part"
        _part.parent.mkdir(parents=True)
        _part.write_bytes(b"x" * len(DELTA_BYTES))
        monkeypatch.setattr(
            _mod.shutil,
            "disk_usage",
            lambda _p: SimpleNamespace(free=len(DELTA_BYTES) - 1),
        )

        with pytest.raises(ota_errors.StandbySlotInsufficientSpace):
            download(_updater, _plan, pool=pool)

    def test_apply_hands_the_unpacked_image_to_the_installer(self, tmp_path: Path):
        _installer = FakeInstaller()
        _updater = make_updater(tmp_path, _installer)
        (tmp_path / "image" / RESOURCE_DIR).mkdir(parents=True)

        _updater.apply(version="2.9.0")

        assert _installer.installed is not None
        assert _installer.installed["package"] == tmp_path / "image"
        assert _installer.installed["version"] == "2.9.0"
        # which payload of a multi-ECU image: the one otaclient downloaded
        assert _installer.installed["ecu_id"] == "autoware"
        assert _installer.installed["release_key"] == "dev"

    def test_a_refused_payload_is_a_failed_update(self, tmp_path: Path):
        _updater = make_updater(
            tmp_path, FakeInstaller(install_error=PartitionInstallError("refused"))
        )
        with pytest.raises(ota_errors.ApplyOTAUpdateFailed, match="did not apply"):
            _updater.apply(version="2.9.0")


#
# ------ the updater otaclient runs ------ #
#


class FakeBootController:
    """What the update asks of boot control, recorded."""

    def __init__(self, *, fail_pre_update: Exception | None = None) -> None:
        self.rebooted = False
        self.pre_updated: tuple[bool, bool] | None = None
        self.post_updated: tuple[str, object] | None = None
        self.failed = False
        self._fail_pre_update = fail_pre_update

    def pre_update(self, *, standby_as_ref: bool, erase_standby: bool) -> None:
        if self._fail_pre_update is not None:
            raise self._fail_pre_update
        self.pre_updated = (standby_as_ref, erase_standby)

    def post_update(self, update_version, *, version_detail=None, **kwargs) -> None:
        self.post_updated = (update_version, version_detail)

    def on_operation_failure(self) -> None:
        self.failed = True

    def finalizing_update(self, *, chroot=None) -> None:
        self.rebooted = True


class FakeAbortHandler:
    def __init__(self, state=None, *, abort_in_zone: bool = False) -> None:
        self.state = state
        self._abort_in_zone = abort_in_zone

    @contextmanager
    def critical_zone(self):
        yield
        if self._abort_in_zone:  # the abort queued during the zone fires on exit
            raise ota_errors.OTAAbortSignal("abort queued", module=__name__)

    def enter_final_phase(self) -> None:
        pass


def make_full_updater(
    tmp_path: Path,
    *,
    pool: DownloaderPool,
    child_in_update: threading.Event | None = None,
    installer: FakeInstaller | None = None,
    boot_controller: FakeBootController | None = None,
    abort_handler: FakeAbortHandler | None = None,
    version: str = "3.0.0",
):
    """OTAUpdaterForPartitionImage with otaclient's own machinery, minus the parts a
    single ECU in a test cannot have: the shared-memory metrics reader and the abort
    handler."""
    _flags = MultipleECUStatusFlags(
        any_child_ecu_in_update=child_in_update or threading.Event(),
        any_requires_network=threading.Event(),
        all_success=threading.Event(),
    )
    return OTAUpdaterForPartitionImage(
        ca_store=CAStoreMap(),
        boot_controller=boot_controller or FakeBootController(),  # type: ignore[arg-type]
        abort_handler=abort_handler or FakeAbortHandler(),  # type: ignore[arg-type]
        image_identifier=THIS_ECU,
        installer=installer or FakeInstaller(),
        staging_dir=tmp_path / "staging",
        version=version,
        raw_url_base=BASE_URL,
        session_wd=tmp_path / "session",
        downloader_pool=pool,
        ecu_status_flags=_flags,
        status_report_queue=Queue(),
        session_id="test",
        metrics=OTAMetricsData(),
        shm_metrics_reader=None,
        release_name="",
        release_id="",
        image_id="",
    )


def staged_dir(tmp_path: Path, version: str = "3.0.0") -> Path:
    """Where `make_full_updater`'s update stages its payload: the version as a
    directory name, under otaclient's own subdirectory."""
    return tmp_path / "staging" / STAGING_SUBDIR / safe_dir_name(version)


@pytest.fixture
def index_verified_from(monkeypatch):
    """Stand in for the index verification, which needs a signed index and a CA
    store: the served image's index files are taken as verified."""

    def _install(served: Path):
        def _verified(self: OTAUpdaterForPartitionImage, condition):
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
    monkeypatch.setattr(_base, "WAIT_BEFORE_REBOOT", 0)


@pytest.fixture
def local_proxy(mocker: MockerFixture):
    """Whether this ECU waits for its child ECUs before rebooting."""

    def _set(enabled: bool):
        mocker.patch.object(
            _base, "proxy_info", SimpleNamespace(enable_local_ota_proxy=enabled)
        )

    return _set


def reports(updater: OTAUpdaterForPartitionImage) -> list:
    _queue = updater._status_report_queue
    return [_queue.get_nowait().payload for _ in range(_queue.qsize())]


class TestExecute:
    """The whole update, from the verified index to the reboot."""

    def test_a_rootfs_release_goes_through_the_installer_to_the_reboot(
        self, tmp_path: Path, pool, server, index_verified_from, no_wait_before_reboot
    ):
        _served, (_manifest, _config_digest) = serve_an_image(
            tmp_path / "served",
            config(
                rootfs(),
                boot(),
                PartitionEntry(name="scratch", action=PartitionAction.mkfs),
                PartitionEntry(name="identity", action=PartitionAction.keep),
                PartitionEntry(name="optdata", action=PartitionAction.keep),
            ),
        )
        server(_served)
        index_verified_from(_served)
        _installer = FakeInstaller()
        _updater = make_full_updater(tmp_path, pool=pool, installer=_installer)

        _updater.execute()

        _boot_controller: FakeBootController = _updater._boot_controller  # type: ignore[assignment]
        # the status files move around the write, and the write is of the standby
        assert _boot_controller.pre_updated == (False, True)
        assert _installer.installed is not None
        assert _installer.installed["package"] == staged_dir(tmp_path) / "image"
        # the installer records the version the payload carries...
        assert _installer.installed["version"] == "2.9.0"
        # ...and otaclient the one the request named
        assert _boot_controller.post_updated == ("3.0.0", None)
        # the rootfs image and the boot files, the manifest and the config
        assert _installer.blobs_seen == sorted(
            ["f" * 64, "c" * 64, _manifest, _config_digest]
        )
        assert _boot_controller.rebooted is True
        assert not staged_dir(tmp_path).exists(), "dead weight once written"
        assert not _boot_controller.failed

        # what the console saw, in order: the phases, the plan, every blob as it
        # landed, the write as bytes
        _reports = reports(_updater)
        assert [
            _r.new_update_phase
            for _r in _reports
            if isinstance(_r, OTAUpdatePhaseChangeReport)
        ] == [
            UpdatePhase.INITIALIZING,  # every updater starts there
            UpdatePhase.PROCESSING_METADATA,
            UpdatePhase.DOWNLOADING_OTA_FILES,
            UpdatePhase.APPLYING_UPDATE,
            UpdatePhase.PROCESSING_POSTUPDATE,
            UpdatePhase.FINALIZING_UPDATE,
        ]
        # the last one: the first carries the version, reported by every updater
        _meta = [_r for _r in _reports if isinstance(_r, SetUpdateMetaReport)][-1]
        assert _meta.total_download_files_num == 2
        assert _meta.total_download_files_size == 1534529536 + 512
        _downloads = [
            _r
            for _r in _reports
            if isinstance(_r, UpdateProgressReport)
            and _r.operation is UpdateProgressReport.Type.DOWNLOAD_REMOTE_COPY
        ]
        # every blob ends as one file of its size, whatever was reported while it
        # downloaded; the traffic is what came over the wire (the served bytes)
        assert [
            _r.processed_file_num for _r in _downloads if _r.processed_file_num
        ] == [
            1,
            1,
        ]
        assert sum(_r.processed_file_size for _r in _downloads) == 1534529536 + 512
        assert sum(_r.downloaded_bytes for _r in _downloads) == 4096 + 512
        _written = sum(
            _r.processed_file_size
            for _r in _reports
            if isinstance(_r, UpdateProgressReport)
            and _r.operation is UpdateProgressReport.Type.APPLY_DELTA
        )
        assert _written == 1534529536 + 512

    def test_a_payload_the_installer_refuses_is_a_failed_update(
        self, tmp_path: Path, pool, server, index_verified_from
    ):
        """The installer's verdict is reported as the failure to apply, the boot
        control records it, and what was downloaded stays for a retry."""
        _served, _ = serve_an_image(tmp_path / "served", config(rootfs(), boot()))
        server(_served)
        index_verified_from(_served)
        _updater = make_full_updater(
            tmp_path,
            pool=pool,
            installer=FakeInstaller(install_error=PartitionInstallError("refused")),
        )

        with pytest.raises(ota_errors.ApplyOTAUpdateFailed, match="did not apply"):
            _updater.execute()

        _boot_controller: FakeBootController = _updater._boot_controller  # type: ignore[assignment]
        assert _boot_controller.failed is True
        assert _boot_controller.rebooted is False
        assert (staged_dir(tmp_path) / "image" / RESOURCE_DIR / ("f" * 64)).is_file()

    def test_an_installer_that_crashes_is_an_unspecific_failure(
        self, tmp_path: Path, pool, server, index_verified_from
    ):
        """Not a refusal, not an OTAError: the one wrapping the updater owns."""
        _served, _ = serve_an_image(tmp_path / "served", config(rootfs(), boot()))
        server(_served)
        index_verified_from(_served)
        _updater = make_full_updater(
            tmp_path,
            pool=pool,
            installer=FakeInstaller(install_error=RuntimeError("boom")),
        )

        with pytest.raises(ota_errors.ApplyOTAUpdateFailed, match="unspecific"):
            _updater.execute()

        assert _updater._boot_controller.failed is True  # type: ignore[attr-defined]

    def test_a_boot_control_that_fails_before_the_write_is_a_pre_update_failure(
        self, tmp_path: Path, pool, server, index_verified_from
    ):
        """E540, as on the file-based path, whatever the boot controller raised."""
        _served, _ = serve_an_image(tmp_path / "served", config(rootfs(), boot()))
        server(_served)
        index_verified_from(_served)
        _installer = FakeInstaller()
        _updater = make_full_updater(
            tmp_path,
            pool=pool,
            installer=_installer,
            boot_controller=FakeBootController(
                fail_pre_update=OSError("no status dir")
            ),
        )

        with pytest.raises(ota_errors.PreUpdateFailed):
            _updater.execute()

        assert _installer.installed is None

    def test_an_error_while_an_abort_is_in_progress_is_the_abort(
        self, tmp_path: Path, pool, server, index_verified_from
    ):
        """The abort handler is cleaning up: the failure is not recorded on top."""
        _served, _ = serve_an_image(tmp_path / "served", config(rootfs(), boot()))
        server(_served)
        index_verified_from(_served)
        _updater = make_full_updater(
            tmp_path,
            pool=pool,
            installer=FakeInstaller(install_error=PartitionInstallError("killed")),
            abort_handler=FakeAbortHandler(state=AbortState.ABORTING),
        )

        with pytest.raises(ota_errors.OTAAbortSignal):
            _updater.execute()

        assert _updater._boot_controller.failed is False  # type: ignore[attr-defined]

    def test_an_abort_queued_during_the_write_fires_after_it(
        self, tmp_path: Path, pool, server, index_verified_from
    ):
        """The write is the critical zone: the abort waits for it, then the signal
        passes through as it is."""
        _served, _ = serve_an_image(tmp_path / "served", config(rootfs(), boot()))
        server(_served)
        index_verified_from(_served)
        _installer = FakeInstaller()
        _updater = make_full_updater(
            tmp_path,
            pool=pool,
            installer=_installer,
            abort_handler=FakeAbortHandler(abort_in_zone=True),
        )

        with pytest.raises(ota_errors.OTAAbortSignal):
            _updater.execute()

        assert _installer.installed is not None
        assert _updater._boot_controller.post_updated is None  # type: ignore[attr-defined]
        assert _updater._boot_controller.failed is False  # type: ignore[attr-defined]

    def test_a_payload_of_data_images_alone_is_refused_before_downloading(
        self, tmp_path: Path, pool, server, index_verified_from
    ):
        """Applying it is a trial boot of the running slot, which the boot control of
        this client does not do: refused as what it is, with nothing fetched."""
        _served, (_manifest, _config_digest) = serve_an_image(
            tmp_path / "served",
            config(
                PartitionEntry(name="rootfs", action=PartitionAction.keep),
                PartitionEntry(name="boot", action=PartitionAction.keep),
                data_images=(data_image(),),
            ),
        )
        server(_served)
        index_verified_from(_served)
        _installer = FakeInstaller()
        _updater = make_full_updater(tmp_path, pool=pool, installer=_installer)

        with pytest.raises(ota_errors.OTAImageInvalid, match="writes no slot"):
            _updater.execute()

        assert _installer.installed is None
        _staged_blobs = staged_dir(tmp_path) / "image" / RESOURCE_DIR
        assert sorted(p.name for p in _staged_blobs.iterdir()) == sorted(
            [_manifest, _config_digest]
        ), "the manifest and the config, not the data image"

    @pytest.mark.parametrize("version", ["/", "../..", "", "a/b", "."])
    def test_the_requested_version_names_a_directory_and_nothing_else(
        self, tmp_path: Path, pool, server, index_verified_from, version: str
    ):
        """The version comes from the request, unauthenticated: it is a directory name
        under otaclient's own staging subdirectory, never a path of its own."""
        _served, _ = serve_an_image(tmp_path / "served", config(rootfs(), boot()))
        server(_served)
        index_verified_from(_served)
        _installer = FakeInstaller(
            install_error=PartitionInstallError("stop here")
        )  # the staging must still be there afterwards
        _updater = make_full_updater(
            tmp_path, pool=pool, installer=_installer, version=version
        )

        with pytest.raises(ota_errors.ApplyOTAUpdateFailed):
            _updater.execute()

        _root = tmp_path / "staging" / STAGING_SUBDIR
        [_only] = list(_root.iterdir())
        assert _only.parent == _root and (_only / "image" / INDEX_JWT_FNAME).is_file()
        assert (tmp_path / "served").is_dir()  # nothing beside the staging was touched


class FakeTraffic:
    """The downloader pool's byte counter, moved by the test."""

    def __init__(self, start: int = 0) -> None:
        self.bytes = start

    def __call__(self) -> int:
        return self.bytes


def planned(size: int, what: str = "the rootfs image") -> _mod.PlannedBlob:
    return _mod.PlannedBlob(descriptor=blob("f" * 64, size=size), what=what)


def sizes_and_traffic(
    reports: list[UpdateProgressReport],
) -> list[tuple[int, int, int]]:
    return [
        (_r.processed_file_num, _r.processed_file_size, _r.downloaded_bytes)
        for _r in reports
    ]


class TestDownloadProgress:
    """A blob of gigabytes is reported while it downloads, not only once whole."""

    def test_a_blob_is_reported_as_it_arrives_and_counted_once_in_place(self):
        _traffic = FakeTraffic(start=100)  # the metadata, reported with the plan
        _reports: list[UpdateProgressReport] = []
        _progress = _mod.BlobDownloadProgress(traffic=_traffic, report=_reports.append)
        _blob = planned(1000)

        _progress.blob_started(_blob)
        _traffic.bytes = 400
        _progress.tick()
        _progress.tick()  # nothing arrived since: nothing to report
        _traffic.bytes = 900
        _progress.tick()
        _progress.blob_done(_blob, True)

        assert sizes_and_traffic(_reports) == [
            (0, 300, 300),
            (0, 500, 500),
            (1, 200, 0),  # the rest of the blob, as one file
        ]

    def test_progress_through_a_blob_never_passes_its_size(self):
        """A retried range comes over the wire twice: traffic, not more blob."""
        _traffic = FakeTraffic()
        _reports: list[UpdateProgressReport] = []
        _progress = _mod.BlobDownloadProgress(traffic=_traffic, report=_reports.append)
        _blob = planned(1000)

        _progress.blob_started(_blob)
        _traffic.bytes = 1500
        _progress.tick()
        _progress.blob_done(_blob, True)

        assert sizes_and_traffic(_reports) == [(0, 1000, 1500), (1, 0, 0)]

    def test_what_an_interrupted_attempt_left_is_progress_from_the_start(self):
        """The download continues it with a ranged request: no traffic for it, and
        no jump at the end."""
        _traffic = FakeTraffic()
        _reports: list[UpdateProgressReport] = []
        _progress = _mod.BlobDownloadProgress(traffic=_traffic, report=_reports.append)
        _blob = planned(1000)

        _progress.blob_started(_blob, 700)
        _traffic.bytes = 250
        _progress.tick()
        _traffic.bytes = 300
        _progress.blob_done(_blob, True)

        assert sizes_and_traffic(_reports) == [
            (0, 700, 0),
            (0, 250, 250),
            (1, 50, 50),
        ]

    def test_a_blob_an_earlier_attempt_left_is_counted_whole_without_traffic(self):
        _reports: list[UpdateProgressReport] = []
        _progress = _mod.BlobDownloadProgress(
            traffic=FakeTraffic(start=100), report=_reports.append
        )
        _blob = planned(1000)

        _progress.blob_started(_blob)
        _progress.blob_done(_blob, False)

        assert sizes_and_traffic(_reports) == [(1, 1000, 0)]

    def test_each_blob_starts_from_nothing(self):
        _traffic = FakeTraffic()
        _reports: list[UpdateProgressReport] = []
        _progress = _mod.BlobDownloadProgress(traffic=_traffic, report=_reports.append)
        _first, _second = planned(1000), planned(10, what="the boot files")

        _progress.blob_started(_first)
        _traffic.bytes = 1000
        _progress.blob_done(_first, True)
        _progress.blob_started(_second)
        _traffic.bytes = 1004
        _progress.tick()
        _progress.blob_done(_second, True)

        assert sizes_and_traffic(_reports) == [
            (1, 1000, 1000),
            (0, 4, 4),
            (1, 6, 0),
        ]

    def test_it_reports_on_its_own_while_the_block_runs_and_stops_after(self):
        _traffic = FakeTraffic()
        _reported = threading.Event()
        _reports: list[UpdateProgressReport] = []

        def _report(_r: UpdateProgressReport) -> None:
            _reports.append(_r)
            _reported.set()

        _progress = _mod.BlobDownloadProgress(
            traffic=_traffic, report=_report, interval=0.01
        )
        _progress.blob_started(planned(1000))
        with _progress.reporting():
            _traffic.bytes = 10
            assert _reported.wait(5), "reported without being asked"
        _traffic.bytes = 20
        threading.Event().wait(0.05)

        assert sizes_and_traffic(_reports) == [(0, 10, 10)]

    def test_a_report_that_fails_does_not_stop_the_download(self, caplog):
        _calls = threading.Event()

        def _report(_r: UpdateProgressReport) -> None:
            _calls.set()
            raise RuntimeError("the status queue is gone")

        _traffic = FakeTraffic()
        _progress = _mod.BlobDownloadProgress(
            traffic=_traffic, report=_report, interval=0.01
        )
        _progress.blob_started(planned(1000))
        with _progress.reporting():
            _traffic.bytes = 10
            assert _calls.wait(5)

        assert "failed to report the download progress" in caplog.text


class TestWriteProgress:
    def test_the_percent_of_the_whole_write_becomes_bytes_once(self, tmp_path, pool):
        """Reported as a difference: a percent that goes back or past 100 adds
        nothing, and the total never exceeds what is written."""
        _updater = make_full_updater(tmp_path, pool=pool)
        for _percent in (0, 50, 50, 30, 120, 100):
            _updater._on_write_progress(_percent, total_bytes=1000)

        _reported = [
            _r.processed_file_size
            for _r in reports(_updater)
            if isinstance(_r, UpdateProgressReport)
        ]
        assert _reported == [500, 500]


class TestFinalize:
    """The last phase: this ECU does not reboot while another is still updating."""

    def test_it_waits_for_a_child_ecu_before_rebooting(
        self, tmp_path: Path, pool, no_wait_before_reboot, local_proxy
    ):
        local_proxy(True)
        _child_updating = threading.Event()
        _child_updating.set()
        _updater = make_full_updater(
            tmp_path, pool=pool, child_in_update=_child_updating
        )
        _staging = staged_dir(tmp_path)
        _staging.mkdir(parents=True)

        _done = threading.Event()
        _error: list[BaseException] = []

        def _run():
            try:
                _updater._finalize(_staging)
            except BaseException as e:  # noqa: BLE001
                _error.append(e)
            finally:
                _done.set()

        threading.Thread(target=_run, daemon=True).start()
        assert not _done.wait(timeout=1)  # the child is still updating: no reboot
        assert _updater._boot_controller.rebooted is False  # type: ignore[attr-defined]

        _child_updating.clear()  # the child is done

        assert _done.wait(timeout=10)
        assert _error == []
        assert _updater._boot_controller.rebooted is True  # type: ignore[attr-defined]

    def test_without_a_local_proxy_there_is_no_child_to_wait_for(
        self, tmp_path: Path, pool, no_wait_before_reboot, local_proxy
    ):
        local_proxy(False)
        _child_updating = threading.Event()
        _child_updating.set()
        _updater = make_full_updater(
            tmp_path, pool=pool, child_in_update=_child_updating
        )
        _staging = staged_dir(tmp_path)
        _staging.mkdir(parents=True)

        _updater._finalize(_staging)

        assert _updater._boot_controller.rebooted is True  # type: ignore[attr-defined]

    def test_what_was_staged_is_removed_once_it_is_written(
        self, tmp_path: Path, pool, no_wait_before_reboot, local_proxy
    ):
        """Gigabytes on persistent storage, of use only to a retry that will not
        happen."""
        local_proxy(False)
        _updater = make_full_updater(tmp_path, pool=pool)
        _staging = staged_dir(tmp_path)
        (_staging / "blobs").mkdir(parents=True)
        (_staging / "blobs" / "big").write_bytes(b"x" * 1024)

        _updater._finalize(_staging)

        assert not _staging.exists()
        assert _updater._boot_controller.rebooted is True  # type: ignore[attr-defined]


class TestStagingRoot:
    """The staging root is pruned and written as root, so it has to be ours."""

    def test_a_private_directory_of_ours_is_accepted(self, tmp_path: Path):
        _root = tmp_path / "staging"
        _root.mkdir(mode=0o700)
        _ensure_the_staging_root_is_ours(_root)

    def test_a_symlink_placed_there_first_is_refused(self, tmp_path: Path):
        """A non-root user who can create the parent (a sticky /var/tmp, say) could
        point the root at /opt/data and have the prune remove what is there."""
        (tmp_path / "elsewhere").mkdir()
        _root = tmp_path / "staging"
        _root.symlink_to(tmp_path / "elsewhere")
        with pytest.raises(ota_errors.ApplyOTAUpdateFailed, match="symlink"):
            _ensure_the_staging_root_is_ours(_root)

    def test_a_directory_others_can_write_is_refused(self, tmp_path: Path):
        _root = tmp_path / "staging"
        _root.mkdir()
        _root.chmod(0o777)
        with pytest.raises(ota_errors.ApplyOTAUpdateFailed, match="not a private"):
            _ensure_the_staging_root_is_ours(_root)


class TestStaging:
    def test_an_earlier_updates_staging_is_cleared_before_a_new_one(
        self, tmp_path: Path, pool
    ):
        """Each update stages under its own version; last time's is dead weight."""
        _updater = make_full_updater(tmp_path, pool=pool)
        _old = staged_dir(tmp_path, "2.9.0")
        _old.mkdir(parents=True)
        _keep = staged_dir(tmp_path)
        _keep.mkdir(parents=True)

        _updater._prune_staging(keep=_keep)

        assert not _old.exists()
        assert _keep.exists()

    def test_what_the_boot_controller_keeps_beside_otaclients_corner_is_left_alone(
        self, tmp_path: Path, pool
    ):
        """The staging directory is the boot controller's; otaclient prunes only its
        own subdirectory of it."""
        _updater = make_full_updater(tmp_path, pool=pool)
        _theirs = tmp_path / "staging" / "records"
        _theirs.mkdir(parents=True)
        _keep = staged_dir(tmp_path)
        _keep.mkdir(parents=True)

        _updater._prune_staging(keep=_keep)

        assert _theirs.exists()

    def test_the_refusal_of_a_payload_that_writes_no_slot(self):
        _refuse = OTAUpdaterForPartitionImage._refuse_what_this_boot_control_cannot_try
        with pytest.raises(ota_errors.OTAImageInvalid):
            _refuse(
                config(
                    PartitionEntry(name="rootfs", action=PartitionAction.keep),
                    data_images=(data_image(),),
                )
            )
        # a vendor package writes the slot, if not by the agent
        _refuse(config(vendor_written("rootfs"), delivery=DeliveryMode.vendor_package))
        _refuse(config(rootfs(), boot()))


class TestTheUpdateAgentTheImageShips:
    """The consumer that installs a bundle from the agent package is the installer,
    and otaclient is what fetches. An entry the index names and the blobs do not
    carry is an image the installer refuses after otaclient has downloaded
    gigabytes."""

    @staticmethod
    def agent_manifest(*bundles):
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
            vendor_written("rootfs"), delivery=DeliveryMode.vendor_package
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
            vendor_written("rootfs"), delivery=DeliveryMode.vendor_package
        )
        assert [_b.what for _b in _updater.plan().blobs] == ["the vendor package"]
