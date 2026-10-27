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
"""Applying a partition-based OTA image payload (ota-image-libs, spec/partition_image.md).

The file-based path rebuilds the standby slot file by file, which is why it computes
a delta against what the slot already holds and downloads resources by digest. A
partition-based payload is different in kind: the standby slot is a read-only image
with a dm-verity hash tree appended, formatted in the cloud and written as bytes, so
there is nothing to rebuild and nothing to mount. What otaclient does here is the
consumer's part that touches no partition: verify the image index as for any version
1 image, select this ECU's payload, decide *which* of its blobs this device needs,
fetch those, and hand the result to the partition installer of this device's boot
control (`PartitionInstallerProtocol`), which owns everything that touches a
partition.

The saving that file-level deduplication gives the other path comes from somewhere
else here: a partition may ship a block diff against the exact bytes of the committed
slot, named by digest. Whether it fits is a question only the device can answer, and
it is asked of the installer before anything is downloaded: a delta that does not fit
is a payload this device cannot use.

What is downloaded is laid out as an unpacked OTA image -- `index.json`, `index.jwt`
and `blobs/sha256/<digest>` -- because that is what an installer reads: it verifies
`index.jwt` again on the device, so the bytes it is given are the ones published.
The blobs are fetched through the same download helper as the metafiles of a
file-based update, so a stalled or failing download is retried, watched and reported
with the same error codes.

The sys_config of a partition-based payload is informational (the root filesystem is
read-only; what it lists was applied when the image was built), so nothing of it is
installed here, and the ecu_info.yaml / proxy_info.yaml of the image are not either:
the standby slot is never mounted on this path.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import threading
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path

from ota_image_libs.common import OCIDescriptor
from ota_image_libs.common.io import file_sha256
from ota_image_libs.v1.consts import IMAGE_INDEX_FNAME, INDEX_JWT_FNAME, RESOURCE_DIR
from ota_image_libs.v1.image_index.schema import ImageIndex
from ota_image_libs.v1.image_manifest.schema import ImageIdentifier
from ota_image_libs.v1.partition_image.schema import (
    DataImageEntry,
    DeliveryMode,
    PartitionAction,
    PartitionEntry,
    PartitionImageConfig,
    PartitionImageManifest,
)
from ota_image_libs.v1.update_agent_package.schema import (
    UpdateAgentPackageManifest,
)
from typing_extensions import Unpack

from ota_metadata.utils.cert_store import CAStoreMap
from otaclient import errors as ota_errors
from otaclient._status_monitor import (
    SetUpdateMetaReport,
    StatusReport,
    UpdateProgressReport,
)
from otaclient._types import AbortState, UpdatePhase
from otaclient._utils import safe_dir_name
from otaclient.boot_control.protocol import (
    BootControllerProtocol,
    PartitionInstallerProtocol,
    PartitionInstallError,
)
from otaclient.configs.cfg import ecu_info
from otaclient.ota_core._common import download_exception_handler
from otaclient.ota_core._download_resources import PARTIAL_SUFFIX
from otaclient.ota_core._update_libs import (
    metadata_download_err_handler,
    raise_on_download_resources_failed,
)
from otaclient_common import human_readable_size
from otaclient_common.common import urljoin_ensure_base
from otaclient_common.download_info import DownloadInfo
from otaclient_common.downloader import resumable_bytes

from ._abort_handler import AbortHandler
from ._updater_base import OTAUpdateInitializer, OTAUpdateInterfaceArgs

logger = logging.getLogger(__name__)


def _ensure_the_staging_root_is_ours(root: Path) -> None:
    """The staging root is pruned and written as root: it has to be a directory of
    our own. A symlink someone placed there first, or a directory another user owns
    or can write, would turn the prune into a removal of whatever it points at."""
    _st = os.lstat(root)
    if stat.S_ISLNK(_st.st_mode):
        raise ota_errors.ApplyOTAUpdateFailed(
            f"the staging root {root} is a symlink; refusing to stage there",
            module=__name__,
        )
    if (
        not stat.S_ISDIR(_st.st_mode)
        or _st.st_uid != os.geteuid()
        or _st.st_mode & 0o022
    ):
        raise ota_errors.ApplyOTAUpdateFailed(
            f"the staging root {root} is not a private directory of ours "
            f"(mode {stat.S_IMODE(_st.st_mode):o}, uid {_st.st_uid}); refusing to "
            "stage there",
            module=__name__,
        )


def _digest_of(path: Path) -> str:
    return file_sha256(path).hexdigest()


STAGING_SUBDIR = "staged"
"""otaclient's own subdirectory of the staging directory the boot controller
provides: everything under it is otaclient's to create and remove, whatever else the
boot controller keeps beside it."""


@dataclass(frozen=True)
class PlannedBlob:
    """One blob this device turns out to need, and where it goes."""

    descriptor: OCIDescriptor
    what: str
    """A label for the log, e.g. "the rootfs delta" against "the rootfs image": a
    reader deciding whether a campaign behaved has to see what the device chose."""

    @property
    def digest_hex(self) -> str:
        return self.descriptor.digest.digest_hex

    @property
    def size(self) -> int:
        return self.descriptor.size


@dataclass(frozen=True)
class DownloadPlan:
    """Everything to fetch for this update, after the device has been consulted."""

    blobs: tuple[PlannedBlob, ...]

    @property
    def total_size(self) -> int:
        return sum(_b.size for _b in self.blobs)


OnBlobDone = Callable[[PlannedBlob, bool], None]
"""Called as each planned blob is in place: the blob, and whether it was downloaded
(False: it was there from an earlier attempt and hashed to its digest)."""


class PartitionImageUpdater:
    """Fetches what this device needs of a partition-based payload, then applies it.

    Nothing here opens a partition, computes a hash tree or decides a slot: those are
    the installer's, and the division is deliberate -- the rules about what may be
    written where live in one place, on the device.

    The downloads are produced as batches of `DownloadInfo` for
    `DownloadHelperForOTAImageV1.download_meta_files`, as `OTAImageHelper` produces
    the metafiles: a batch is yielded under the condition and the generator waits on
    it until the helper has downloaded the batch.
    """

    def __init__(
        self,
        *,
        base_url: str,
        session_dir: Path,
        image_id: ImageIdentifier,
        installer: PartitionInstallerProtocol,
    ) -> None:
        self._blob_base_url = urljoin_ensure_base(base_url, RESOURCE_DIR)
        self._installer = installer
        self._image_id = image_id

        # The unpacked image otaclient assembles and the installer reads.
        self.image_dir = session_dir / "image"
        self._blob_dir = self.image_dir / RESOURCE_DIR

        self.image_config: PartitionImageConfig | None = None
        self.update_agent_manifest: UpdateAgentPackageManifest | None = None

    # ------ blobs ------ #

    def _blob_is_here(self, descriptor: OCIDescriptor, *, what: str) -> bool:
        """Whether the unpacked image already holds this blob, whole and verified.

        A complete blob is reused after hashing it again (the size alone does not
        prove its contents); one of another size, or of the right size but other
        bytes, is not this blob and is removed.
        """
        _digest = descriptor.digest.digest_hex
        _dst = self._blob_dir / _digest
        if not _dst.is_file():
            return False
        if _dst.stat().st_size == descriptor.size and _digest_of(_dst) == _digest:
            logger.info(f"{what} is already downloaded: {_digest[:12]}...")
            return True
        logger.warning(
            f"{what} was downloaded before but is not {_digest[:12]}...; "
            "downloading it again"
        )
        _dst.unlink(missing_ok=True)
        return False

    def _fetch_blob(
        self, descriptor: OCIDescriptor, *, what: str, condition: threading.Condition
    ) -> Generator[list[DownloadInfo], None, bool]:
        """Yield the download of one blob, unless the unpacked image holds it.

        Bytes arrive in `<digest>.part` and become `<digest>` once the helper has
        downloaded the whole file and it hashed to the digest (the downloader checks
        both before returning, and the helper retries until it does). What an
        interrupted attempt left in `<digest>.part` -- the attempt before, or an
        earlier update of the same version, whose staging is kept -- is continued
        with a ranged request rather than downloaded again: a rootfs image is
        gigabytes, and a link that drops at 90% should not cost all of it. The
        digest is still computed over the whole file, so the resumed part is trusted
        no further than a fresh one.

        Returns:
            Whether the blob was downloaded (False: reused).
        """
        if self._blob_is_here(descriptor, what=what):
            return False
        _digest = descriptor.digest.digest_hex
        _dst = self._blob_dir / _digest
        _part = _dst.with_name(_dst.name + PARTIAL_SUFFIX)
        _dst.parent.mkdir(exist_ok=True, parents=True)
        with condition:
            yield [
                DownloadInfo(
                    url=urljoin_ensure_base(self._blob_base_url, _digest),
                    dst=_part,
                    original_size=descriptor.size,
                    digest=_digest,
                    resume=True,
                )
            ]
            condition.wait()
        # Whole and verified; from here on it is a blob, not a download.
        _part.replace(_dst)
        return True

    # ------ metadata ------ #

    def download_metadata(
        self,
        image_index: ImageIndex,
        *,
        index_json: Path,
        index_jwt: Path,
        condition: threading.Condition,
    ) -> Generator[list[DownloadInfo], None, None]:
        """Fetch this ECU's payload manifest and config from the verified image
        index; afterwards `image_config` (and `update_agent_manifest`, when the
        image ships one) are set.

        `index.json` and `index.jwt` go into the unpacked image as they were
        downloaded, beside the blobs: the installer verifies the signature again on
        the device, against its own trust anchor, and the bytes it is given are what
        that check is of.
        """
        self.image_dir.mkdir(exist_ok=True, parents=True)
        shutil.copyfile(index_jwt, self.image_dir / INDEX_JWT_FNAME)
        shutil.copyfile(index_json, self.image_dir / IMAGE_INDEX_FNAME)

        _manifest_descriptor = image_index.find_partition_image(self._image_id)
        if _manifest_descriptor is None:
            raise ota_errors.OTAImageInvalid(
                f"the OTA image carries no partition-based payload for {self._image_id}",
                module=__name__,
            )

        yield from self._fetch_blob(
            _manifest_descriptor, what="the payload manifest", condition=condition
        )
        try:
            _manifest = PartitionImageManifest.parse_metafile(
                (self._blob_dir / _manifest_descriptor.digest.digest_hex).read_text()
            )
        except Exception as e:
            raise ota_errors.OTAImageInvalid(
                f"the payload manifest is invalid: {e!r}", module=__name__
            ) from e

        yield from self._fetch_blob(
            _manifest.config, what="the payload config", condition=condition
        )
        try:
            _config = PartitionImageConfig.parse_metafile(
                (self._blob_dir / _manifest.config.digest.digest_hex).read_text()
            )
        except Exception as e:
            raise ota_errors.OTAImageInvalid(
                f"the payload config is invalid: {e!r}", module=__name__
            ) from e

        # The update agent package, when the image ships one. Its manifest goes into
        # the unpacked image beside the payload's, because the installer reads the
        # index and follows it: an entry the index names and the blobs do not carry
        # is an image the installer refuses, and it is otaclient that decides what is
        # fetched.
        _agent_descriptor = image_index.find_update_agent_package()
        if _agent_descriptor is not None:
            yield from self._fetch_blob(
                _agent_descriptor,
                what="the update agent package manifest",
                condition=condition,
            )
            try:
                self.update_agent_manifest = UpdateAgentPackageManifest.parse_metafile(
                    (self._blob_dir / _agent_descriptor.digest.digest_hex).read_text()
                )
            except Exception as e:
                raise ota_errors.OTAImageInvalid(
                    f"the update agent package is invalid: {e!r}", module=__name__
                ) from e

        self.image_config = _config
        logger.info(
            f"partition-based payload {_config.image_version} for {self._image_id}, "
            f"delivery {_config.delivery}"
        )

    # ------ what to fetch ------ #

    def plan(self) -> DownloadPlan:
        """Decide what to fetch: every blob the payload ships, as the config lists
        them (`payload_descriptors`), with one difference -- a partition or data
        image that ships a delta is fetched as the delta only after the device has
        confirmed it holds the bytes it applies to, and refused here otherwise, rather
        than after a gigabyte of downloading: a delta payload carries no whole image.
        """
        _config = self.image_config
        if _config is None:
            raise ValueError("download_metadata() has not run")

        _blobs: list[PlannedBlob] = []
        if _config.delivery is DeliveryMode.direct:
            for _partition in _config.partitions:
                if _partition.action is not PartitionAction.write:
                    continue  # mkfs and keep carry nothing to download
                assert (
                    _partition.image is not None
                )  # the schema refuses a write by the agent without one
                _blobs.append(
                    self._image_or_delta(_partition, f"the {_partition.name}")
                )
        # Data images and firmware ride under either delivery: a vendor package
        # writes the partitions, the installer places these.
        for _data in _config.data_images:
            _blobs.append(
                self._image_or_delta(
                    _data, f"the {_data.name} data image", data_image=_data.name
                )
            )
        if (_firmware := _config.firmware) is not None:
            _blobs.append(
                PlannedBlob(
                    _firmware.package,
                    f"the {_firmware.name} firmware package {_firmware.version}",
                )
            )
        if _config.delivery is DeliveryMode.vendor_package:
            # One opaque package that the platform's own updater applies; otaclient
            # fetches it and the installer hands it over.
            assert _config.package is not None  # the schema refuses the pair
            _blobs.append(PlannedBlob(_config.package, "the vendor package"))
        # Every agent bundle the image ships: which one to install is the installer's
        # choice, and they are megabytes beside a payload of gigabytes.
        if self.update_agent_manifest is not None:
            _blobs.extend(
                PlannedBlob(
                    _b, f"the {_b.annotations.type} agent {_b.annotations.version}"
                )
                for _b in self.update_agent_manifest.layers
            )
        return DownloadPlan(blobs=tuple(_blobs))

    def _image_or_delta(
        self,
        entry: PartitionEntry | DataImageEntry,
        what: str,
        *,
        data_image: str | None = None,
    ) -> PlannedBlob:
        """The delta when the entry ships one and the device holds its source, else
        the image; a delta the device cannot apply is a refusal."""
        assert entry.image is not None
        if entry.delta is None:
            return PlannedBlob(entry.image, f"{what} image")
        if self._delta_fits(entry, data_image=data_image):
            return PlannedBlob(entry.delta, f"{what} delta")
        raise ota_errors.ApplyOTAUpdateFailed(
            f"{what} delta does not apply to this device: it does not hold the bytes "
            "the delta was built from",
            module=__name__,
        )

    def _delta_fits(
        self,
        entry: PartitionEntry | DataImageEntry,
        *,
        data_image: str | None = None,
    ) -> bool:
        """Whether the committed slot (or the named data image's file) holds the bytes
        the delta names, as the installer hashes them; an installer that cannot
        answer is a refusal."""
        _delta = entry.delta
        assert _delta is not None
        try:
            _on_device = self._installer.source_digest(
                size=_delta.annotations.source_size, data_image=data_image
            )
        except PartitionInstallError as e:
            logger.warning(f"cannot tell whether the {entry.name} delta applies: {e!r}")
            return False

        _wanted_hex = _delta.annotations.source_digest.split(":")[-1]
        if _on_device == _wanted_hex:
            logger.info(f"the {entry.name} delta applies to this device")
            return True
        logger.info(
            f"the {entry.name} delta applies to {_wanted_hex[:12]}... but this device "
            f"holds {_on_device[:12]}..."
        )
        return False

    # ------ fetching and applying ------ #

    def download_payload(
        self,
        plan: DownloadPlan,
        *,
        condition: threading.Condition,
        on_blob_done: OnBlobDone | None = None,
    ) -> Generator[list[DownloadInfo], None, None]:
        """Yield the downloads of the planned blobs, one at a time, in plan order."""
        self._check_there_is_room_for(plan)
        for _blob in plan.blobs:
            _downloaded = yield from self._fetch_blob(
                _blob.descriptor, what=_blob.what, condition=condition
            )
            if _downloaded:
                logger.info(
                    f"downloaded {_blob.what}: {human_readable_size(_blob.size)}"
                )
            if on_blob_done is not None:
                on_blob_done(_blob, _downloaded)

    def _check_there_is_room_for(self, plan: DownloadPlan) -> None:
        """Refuse before downloading rather than partway through: the rootfs image or
        delta is most of the payload, and a device with no room should be reported as
        one. A blob of the right size is taken as there; one that then turns out not
        to hash to its digest is downloaded again, and the disk filling meanwhile is
        reported by the download itself. What an interrupted download left in
        `<digest>.part` is continued, so only the rest of that blob needs room."""
        self.image_dir.mkdir(exist_ok=True, parents=True)
        _free = shutil.disk_usage(self.image_dir).free
        _have = 0
        for _b in plan.blobs:
            _dst = self._blob_dir / _b.digest_hex
            _part = _dst.with_name(_dst.name + PARTIAL_SUFFIX)
            if _dst.is_file() and _dst.stat().st_size == _b.size:
                _have += _b.size
            else:
                _have += resumable_bytes(_part, _b.size)
        _needed = plan.total_size - _have
        if _needed > _free:
            raise ota_errors.StandbySlotInsufficientSpace(
                f"the payload needs {human_readable_size(_needed)} staged in "
                f"{self.image_dir}, which has {human_readable_size(_free)} free",
                module=__name__,
            )

    def apply(
        self, *, version: str, on_progress: Callable[[int], None] | None = None
    ) -> None:
        """Hand the unpacked image to the installer, which writes it and arms the
        trial boot."""
        try:
            self._installer.install(
                package=self.image_dir,
                version=version,
                # An image built for a vehicle carries one payload per ECU and the
                # installer refuses to choose between them; this is the same answer
                # otaclient used to pick the payload it downloaded, so the two cannot
                # disagree.
                ecu_id=self._image_id.ecu_id,
                release_key=self._image_id.release_key.value,
                on_progress=on_progress,
            )
        except PartitionInstallError as e:
            raise ota_errors.ApplyOTAUpdateFailed(
                f"the installer did not apply the payload: {e!r}", module=__name__
            ) from e


#
# ------ the updater otaclient runs ------ #
#


class OTAUpdaterForPartitionImage(OTAUpdateInitializer):
    """An OTA update that writes partition images, from otaclient's side.

    The phases are the ones every update reports -- metadata, downloading, applying,
    post-update, finalizing -- so the console and the metrics read the same as any
    other. What happens inside them is different only where the payload is: nothing
    is mounted, the delta is decided by asking the device, and the writing is the
    installer's.
    """

    def __init__(
        self,
        *,
        ca_store: CAStoreMap,
        boot_controller: BootControllerProtocol,
        abort_handler: AbortHandler,
        image_identifier: ImageIdentifier,
        installer: PartitionInstallerProtocol,
        staging_dir: str | Path,
        **kwargs: Unpack[OTAUpdateInterfaceArgs],
    ) -> None:
        super().__init__(**kwargs)
        self._boot_controller = boot_controller
        self._abort_handler = abort_handler
        self._image_id = image_identifier
        self._installer = installer
        # otaclient's own corner of what the boot controller provides: pruned freely.
        self._staging_root = Path(staging_dir) / STAGING_SUBDIR
        self._written_bytes_reported = 0

        # The index is verified as every version 1 image's is, against this ECU's
        # own CA store, before anything in it is believed.
        self._ota_image_helper, self._download_helper = (
            self._create_ota_image_v1_helpers(ca_store=ca_store)
        )

    # ------ reporting ------ #

    def _on_blob_done(self, blob: PlannedBlob, downloaded: bool) -> None:
        """One planned blob is in place: counted as a file either way, as traffic
        only when it was downloaded."""
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=UpdateProgressReport(
                    operation=UpdateProgressReport.Type.DOWNLOAD_REMOTE_COPY,
                    processed_file_num=1,
                    processed_file_size=blob.size,
                    downloaded_bytes=blob.size if downloaded else 0,
                ),
                session_id=self.session_id,
            )
        )

    def _on_write_progress(self, percent: int, *, total_bytes: int) -> None:
        """The installer reports percent of the whole write; otaclient reports bytes.

        Reported as a difference so the console's total keeps climbing rather than
        restarting, which is what the file-based path does for every file it writes.
        """
        _so_far = total_bytes * max(0, min(100, percent)) // 100
        _delta = _so_far - self._written_bytes_reported
        if _delta <= 0:
            return
        self._written_bytes_reported = _so_far
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=UpdateProgressReport(
                    operation=UpdateProgressReport.Type.APPLY_DELTA,
                    processed_file_size=_delta,
                ),
                session_id=self.session_id,
            )
        )

    # ------ staging ------ #

    def _session_staging_dir(self) -> Path:
        """Where this update stages its payload: under otaclient's own staging
        subdirectory, named after the requested version.

        The version comes from the update request, so it is only ever a directory
        name here (`safe_dir_name`), never a path: `..` or an absolute version would
        otherwise stage, and later remove, somewhere else.
        """
        _dir = self._staging_root / safe_dir_name(self.update_version)
        if _dir.parent != self._staging_root:  # pragma: no cover
            raise ota_errors.InvalidUpdateRequest(
                f"version {self.update_version!r} cannot name a staging directory",
                module=__name__,
            )
        return _dir

    def _prune_staging(self, keep: Path) -> None:
        """Last update's payload is dead weight on persistent storage; this one's
        may be a retry.

        Only what this update staged is kept. The blobs are named by digest, so a
        retry of the same version finds its downloads where it left them. Nothing
        outside otaclient's own subdirectory is touched.
        """
        for _entry in self._staging_root.iterdir():
            if _entry == keep or not _entry.is_dir():
                continue
            logger.info(f"removing the payload staged for an earlier update: {_entry}")
            shutil.rmtree(_entry, ignore_errors=True)

    # ------ the metadata ------ #

    def _verify_image_index(self, condition: threading.Condition) -> ImageIndex:
        """`index.jwt` and `index.json`, downloaded into the session directory and
        verified against this ECU's CA store, as for any version 1 image."""
        for _fut in self._download_helper.download_meta_files(
            self._ota_image_helper.download_and_verify_image_index(condition=condition),
            condition=condition,
        ):
            download_exception_handler(_fut)
        _index = self._ota_image_helper.image_index
        if _index is None:  # the helper raises rather than leaving it unset
            raise ota_errors.OTAImageInvalid(
                "the OTA image index was not read", module=__name__
            )
        return _index

    @property
    def _verified_index_files(self) -> tuple[Path, Path]:
        """`index.json` and `index.jwt` as `_verify_image_index` left them."""
        return (
            self._session_workdir / IMAGE_INDEX_FNAME,
            self._session_workdir / INDEX_JWT_FNAME,
        )

    @staticmethod
    def _refuse_what_this_boot_control_cannot_try(
        config: PartitionImageConfig,
    ) -> None:
        """A payload that writes no slot (data images or firmware alone) is tried by
        rebooting the running slot. The status files of a slot switch cannot describe
        that boot, and this client's boot control has no other kind of trial, so such
        a payload is refused before anything is downloaded rather than applied and
        then reported as a failed switch."""
        if all(_p.action is PartitionAction.keep for _p in config.partitions):
            raise ota_errors.OTAImageInvalid(
                "the payload writes no slot (data images or firmware alone), which is "
                "applied by a trial boot of the running slot; this client tries a "
                "slot switch only",
                module=__name__,
            )

    @staticmethod
    def _bytes_to_write(config: PartitionImageConfig) -> int:
        """What the installer ends up writing: every partition image and data image,
        at the size it decodes to (a compressed blob carries that in its
        annotations); the boot files tar is unpacked as it is."""
        _total = 0
        for _p in config.partitions:
            if _p.action is PartitionAction.write and _p.image is not None:
                _total += getattr(_p.image, "image_size", _p.image.size)
        for _d in config.data_images:
            _total += _d.image.image_size
        return _total

    # ------ the update ------ #

    def execute(self) -> None:
        logger.info(
            f"execute local partition-image update({ecu_info.ecu_id=}): "
            f"{self.update_version=}, {self.release_name=}, {self.release_id=}"
        )
        _session_staging = self._session_staging_dir()
        try:
            self._staging_root.mkdir(exist_ok=True, parents=True, mode=0o700)
            _ensure_the_staging_root_is_ours(self._staging_root)
            self._prune_staging(keep=_session_staging)
            _session_staging.mkdir(exist_ok=True, mode=0o700)

            _updater = PartitionImageUpdater(
                base_url=self.url_base,
                session_dir=_session_staging,
                image_id=self._image_id,
                installer=self._installer,
            )
            _condition = threading.Condition()

            # ------ metadata ------ #
            _now = self._report_phase(UpdatePhase.PROCESSING_METADATA)
            self._metrics.processing_metadata_start_timestamp = _now
            with metadata_download_err_handler():
                _index = self._verify_image_index(_condition)
                _index_json, _index_jwt = self._verified_index_files
                for _fut in self._download_helper.download_meta_files(
                    _updater.download_metadata(
                        _index,
                        index_json=_index_json,
                        index_jwt=_index_jwt,
                        condition=_condition,
                    ),
                    condition=_condition,
                ):
                    download_exception_handler(_fut)
            _config = _updater.image_config
            assert _config is not None  # download_metadata() sets it or raises
            self._refuse_what_this_boot_control_cannot_try(_config)

            # ------ what this device needs ------ #
            _plan = _updater.plan()
            _total_write = self._bytes_to_write(_config)
            logger.info(
                f"this device needs {len(_plan.blobs)} blobs, "
                f"{human_readable_size(_plan.total_size)} on the wire, "
                f"for {human_readable_size(_total_write)} written"
            )
            self._status_report_queue.put_nowait(
                StatusReport(
                    payload=SetUpdateMetaReport(
                        total_download_files_num=len(_plan.blobs),
                        total_download_files_size=_plan.total_size,
                        metadata_downloaded_bytes=self._downloader_pool.total_downloaded_bytes,
                    ),
                    session_id=self.session_id,
                )
            )
            self._metrics.delta_download_files_num = len(_plan.blobs)
            self._metrics.delta_download_files_size = _plan.total_size

            # ------ downloading ------ #
            _now = self._report_phase(UpdatePhase.DOWNLOADING_OTA_FILES)
            self._metrics.download_start_timestamp = _now
            try:
                for _fut in self._download_helper.download_meta_files(
                    _updater.download_payload(
                        _plan, condition=_condition, on_blob_done=self._on_blob_done
                    ),
                    condition=_condition,
                ):
                    download_exception_handler(_fut)
            except Exception as e:
                raise_on_download_resources_failed(e)

            # ------ writing ------ #
            # The installer arms the trial boot while it writes (the boot files and
            # the rootfs image are one build, written together or not at all), so an
            # abort that arrives during the write waits for the write to end: a
            # standby slot half written is safe to abandon, a slot armed before its
            # record is written is not.
            with self._abort_handler.critical_zone():
                with ota_errors.raise_as_ota_error(
                    ota_errors.PreUpdateFailed, "pre-update failed", module=__name__
                ):
                    self._boot_controller.pre_update(
                        standby_as_ref=False, erase_standby=True
                    )

                _now = self._report_phase(UpdatePhase.APPLYING_UPDATE)
                self._metrics.apply_update_start_timestamp = _now
                _updater.apply(
                    # The version the payload carries is what the installer records
                    # and the device reports for the image; otaclient's own record
                    # of the update is the version the request named.
                    version=_config.image_version,
                    on_progress=lambda _p: self._on_write_progress(
                        _p, total_bytes=_total_write
                    ),
                )

            self._abort_handler.enter_final_phase()

            # ------ post update ------ #
            _now = self._report_phase(UpdatePhase.PROCESSING_POSTUPDATE)
            self._metrics.post_update_start_timestamp = _now
            with ota_errors.raise_as_ota_error(
                ota_errors.PostUpdateFailed, "post-update failed", module=__name__
            ):
                self._boot_controller.post_update(
                    self.update_version, version_detail=self._version_detail
                )

            self._finalize(_session_staging)

        except ota_errors.OTAAbortSignal:
            logger.info("OTA update aborted")
            raise
        except ota_errors.OTAError as e:
            if self._abort_handler.state in (AbortState.ABORTING, AbortState.ABORTED):
                logger.info(f"OTA update aborted (error during shutdown: {e!r})")
                raise ota_errors.OTAAbortSignal(
                    "abort in progress", module=__name__
                ) from e
            logger.error(f"update failed: {e!r}")
            self._boot_controller.on_operation_failure()
            raise
        except Exception as e:
            if self._abort_handler.state in (AbortState.ABORTING, AbortState.ABORTED):
                logger.info(f"OTA update aborted (error during shutdown: {e!r})")
                raise ota_errors.OTAAbortSignal(
                    "abort in progress", module=__name__
                ) from e
            self._boot_controller.on_operation_failure()
            raise ota_errors.ApplyOTAUpdateFailed(
                f"unspecific error, update failed: {e!r}", module=__name__
            ) from e
        finally:
            self._downloader_pool.release_instance()
            self._downloader_pool.shutdown()
            shutil.rmtree(self._session_workdir, ignore_errors=True)

    def _finalize(self, staging: Path) -> None:
        """Drop what was staged (the payload is on the standby slot now), then wait
        for the sub ECUs, publish the metrics and reboot into the trial slot."""
        shutil.rmtree(staging, ignore_errors=True)
        self._finalize_update_and_reboot(self._boot_controller)
