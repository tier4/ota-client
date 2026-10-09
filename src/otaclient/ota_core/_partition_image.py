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
"""Applying a partition-based OTA image payload.

The file-based path rebuilds the standby slot file by file, which is why it computes
a delta against what the slot already holds and downloads resources by digest. A
partition-based payload is different in kind: the standby slot is a read-only image
with a dm-verity hash tree appended, formatted in the cloud and written as bytes, so
there is nothing to rebuild and nothing to mount. What otaclient does here is decide
*which* of the payload's blobs this device needs, fetch those, and hand the result to
the DPI, which owns everything that touches a partition.

The saving that file-level deduplication gives the other path comes from somewhere
else here: a partition may ship a block diff against the exact bytes of the committed
slot, named by digest. Whether it fits is a question only the device can answer, and
it is asked of the DPI before anything is downloaded: a delta that does not fit is a
payload this device cannot use.

What is downloaded is laid out as an unpacked OTA image — `index.json`, `index.jwt`
and `blobs/sha256/<digest>` — because that is what the DPI reads.
"""

from __future__ import annotations

import errno
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Union

from ota_image_libs.common import OCIDescriptor
from ota_image_libs.common.io import file_sha256
from ota_image_libs.v1.consts import IMAGE_INDEX_FNAME, INDEX_JWT_FNAME, RESOURCE_DIR
from ota_image_libs.v1.image_index.schema import ImageIndex
from ota_image_libs.v1.image_manifest.schema import ImageIdentifier
from ota_image_libs.v1.partition_image.schema import (
    DeliveryMode,
    PartitionAction,
    PartitionImageConfig,
    PartitionImageManifest,
)
from ota_image_libs.v1.update_agent_package.schema import (
    UpdateAgentPackageManifest,
)
from typing_extensions import Unpack

from otaclient import errors as ota_errors
from otaclient._status_monitor import (
    OTAUpdatePhaseChangeReport,
    SetUpdateMetaReport,
    StatusReport,
    UpdateProgressReport,
)
from otaclient._types import AbortState, UpdatePhase, VersionDetail
from otaclient.boot_control._dpi import DPIClient, DPIError, dpi_from_ecu_info
from otaclient.boot_control._partition_image import PartitionImageBootController
from otaclient.configs import BootloaderType
from otaclient.configs.cfg import ecu_info
from otaclient_common import human_readable_size
from otaclient_common.common import urljoin_ensure_base
from otaclient_common.downloader import Downloader, HashVerificationError

from ._abort_handler import AbortHandler
from ._updater_base import OTAUpdateInitializer, OTAUpdateInterfaceArgs

logger = logging.getLogger(__name__)


def _digest_of(path: Path) -> str:
    return file_sha256(path).hexdigest()


STAGING_DIR = "/opt/data/otaclient/partition-image"
"""Where the payload is assembled, on persistent storage rather than in the session
directory: that one is a tmpfs, and a partition image is gigabytes. On this layout
optdata is outside both slots, so what is staged survives the write it feeds."""

STAGING_DIR_BY_BOOTLOADER = {
    BootloaderType.GRUB_VERITY: STAGING_DIR,
    BootloaderType.JETSON_DPI: "/var/tmp/otaclient/partition-image",
}
"""Per device side, because "outside the slots" is not available everywhere.

On L4T the only partition outside the slots is UDA, which the platform installer
mounts at /opt/data and which is a few hundred megabytes -- room for a record, not
for a payload. What is staged there only has to survive until the reboot, and the
write that consumes it happens before that, so it goes inside the running slot,
where the DPI stages its own copy for the same reason."""

PARTIAL_SUFFIX = ".part"
"""What an unfinished download is called until its digest checks out."""


def staging_dir_for(bootloader: BootloaderType) -> str:
    """Where this device side can hold a payload until the DPI has consumed it."""
    return STAGING_DIR_BY_BOOTLOADER.get(bootloader, STAGING_DIR)


@dataclass(frozen=True)
class PlannedBlob:
    """One blob this device turns out to need, and where it goes."""

    descriptor: OCIDescriptor
    what: str
    """What it is, for the log: a reader deciding whether a campaign behaved has to
    see which of the two the device chose."""

    @property
    def digest_hex(self) -> str:
        return self.descriptor.digest.digest_hex

    @property
    def size(self) -> int:
        return self.descriptor.size


@dataclass(frozen=True)
class DownloadPlan:
    """Everything to fetch for this update, after the device has been consulted."""

    blobs: Tuple[PlannedBlob, ...]

    @property
    def total_size(self) -> int:
        return sum(_b.size for _b in self.blobs)


class PartitionImageUpdater:
    """Fetches what this device needs of a partition-based payload, then applies it.

    Nothing here opens a partition, computes a hash tree or decides a slot: those are
    the DPI's, and the division is deliberate — the rules about what may be written
    where live in one place, on the device, and an image can carry a newer DPI than
    the otaclient driving it.
    """

    def __init__(
        self,
        *,
        base_url: str,
        downloader: Downloader,
        session_dir: Path,
        image_id: ImageIdentifier,
        dpi: Optional[DPIClient] = None,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> None:
        self._base_url = base_url
        self._blob_base_url = urljoin_ensure_base(base_url, RESOURCE_DIR)
        self._downloader = downloader
        self._dpi = dpi if dpi is not None else dpi_from_ecu_info()
        self._image_id = image_id
        self._on_progress = on_progress

        # The unpacked image otaclient assembles and the DPI reads.
        self.image_dir = session_dir / "image"
        self._blob_dir = self.image_dir / RESOURCE_DIR

        self.image_index: Optional[ImageIndex] = None
        self.image_manifest: Optional[PartitionImageManifest] = None
        self.image_config: Optional[PartitionImageConfig] = None
        self.update_agent_manifest: UpdateAgentPackageManifest | None = None

    # ------ metadata ------ #

    def _fetch(
        self,
        *,
        name: str,
        dst: Path,
        digest: Optional[str] = None,
        size: Optional[int] = None,
    ) -> Path:
        dst.parent.mkdir(exist_ok=True, parents=True)
        self._downloader.download(
            urljoin_ensure_base(self._base_url, name),
            dst,
            digest=digest,
            size=size,
        )
        return dst

    def _fetch_blob(self, descriptor: OCIDescriptor, *, what: str) -> Path:
        """A blob, into the place the unpacked image keeps it.

        Bytes arrive in `<digest>.part` and become `<digest>` once the whole file
        hashes to the digest. A complete blob is reused after hashing it again (the
        size alone does not prove its contents); a partial one is continued with a
        ranged request, the digest still computed over the whole file.
        """
        _digest = descriptor.digest.digest_hex
        _dst = self._blob_dir / _digest
        _part = _dst.with_name(_dst.name + PARTIAL_SUFFIX)
        if _dst.is_file() and _dst.stat().st_size == descriptor.size:
            if _digest_of(_dst) == _digest:
                logger.info(f"{what} is already downloaded: {_digest[:12]}…")
                return _dst
            logger.warning(
                f"{what} was downloaded before but does not hash to {_digest[:12]}…; "
                "downloading it again"
            )
            _dst.unlink(missing_ok=True)
        _dst.parent.mkdir(exist_ok=True, parents=True)

        _resume_from = 0
        if _part.is_file():
            _have = _part.stat().st_size
            if 0 < _have < descriptor.size:
                _resume_from = _have
                logger.info(
                    f"{what}: {human_readable_size(_have)} of "
                    f"{human_readable_size(descriptor.size)} is already here; "
                    "continuing from there"
                )
            else:
                # Nothing to continue, or more bytes than the blob has: either way not
                # a prefix of it.
                _part.unlink(missing_ok=True)

        try:
            self._downloader.download(
                urljoin_ensure_base(self._blob_base_url, _digest),
                _part,
                digest=_digest,
                size=descriptor.size,
                resume_from=_resume_from,
            )
        except HashVerificationError:
            # The file does not hash to the digest, so it is not this blob — not in
            # whole and not in part. Resuming from it would build on bytes already
            # known to be wrong.
            _part.unlink(missing_ok=True)
            raise
        # Whole and verified; from here on it is a blob, not a download.
        _part.replace(_dst)
        return _dst

    def download_metadata(self) -> PartitionImageConfig:
        """index.jwt, index.json and this ECU's payload manifest and config.

        They go into the unpacked image beside the blobs, unchanged, because the DPI
        verifies `index.jwt` again on the device against its own trust anchor: a
        signature otaclient checked in its own process is not evidence to the thing
        that writes the partition.
        """
        self._fetch(name=INDEX_JWT_FNAME, dst=self.image_dir / INDEX_JWT_FNAME)
        _index_fpath = self._fetch(
            name=IMAGE_INDEX_FNAME, dst=self.image_dir / IMAGE_INDEX_FNAME
        )
        try:
            self.image_index = _index = ImageIndex.parse_metafile(
                _index_fpath.read_text()
            )
        except Exception as e:
            raise ota_errors.MetadataJWTInvalid(
                f"the OTA image index is invalid: {e!r}", module=__name__
            ) from e

        _manifest_descriptor = _index.find_partition_image(self._image_id)
        if _manifest_descriptor is None:
            raise ota_errors.MetadataJWTInvalid(
                f"the OTA image carries no partition-based payload for {self._image_id}",
                module=__name__,
            )

        _manifest_fpath = self._fetch_blob(
            _manifest_descriptor, what="the payload manifest"
        )
        try:
            self.image_manifest = _manifest = PartitionImageManifest.parse_metafile(
                _manifest_fpath.read_text()
            )
        except Exception as e:
            raise ota_errors.MetadataJWTInvalid(
                f"the payload manifest is invalid: {e!r}", module=__name__
            ) from e

        _config_fpath = self._fetch_blob(_manifest.config, what="the payload config")
        try:
            self.image_config = _config = PartitionImageConfig.parse_metafile(
                _config_fpath.read_text()
            )
        except Exception as e:
            raise ota_errors.MetadataJWTInvalid(
                f"the payload config is invalid: {e!r}", module=__name__
            ) from e

        # The update agent package, when the image ships one. Its manifest goes into
        # the unpacked image beside the payload's, because the DPI reads the index and
        # follows it: an entry the index names and the blobs do not carry is an image
        # the DPI refuses, and it is otaclient that decides what is fetched.
        _agent_descriptor = _index.find_update_agent_package()
        if _agent_descriptor is not None:
            _agent_fpath = self._fetch_blob(
                _agent_descriptor, what="the update agent package manifest"
            )
            try:
                self.update_agent_manifest = UpdateAgentPackageManifest.parse_metafile(
                    _agent_fpath.read_text()
                )
            except Exception as e:
                raise ota_errors.MetadataJWTInvalid(
                    f"the update agent package is invalid: {e!r}", module=__name__
                ) from e

        logger.info(
            f"partition-based payload {_config.image_version} for {self._image_id}, "
            f"delivery {_config.delivery}"
        )
        return _config

    # ------ what to fetch ------ #

    def plan(self) -> DownloadPlan:
        """What to fetch, per written partition: the delta when the entry ships one --
        after the device has confirmed it holds the bytes it applies to -- else the
        image. A delta that does not fit is refused here rather than after a gigabyte
        of downloading: a delta payload carries no whole image."""
        _config = self.image_config
        if _config is None:
            raise ValueError("download_metadata() has not run")
        if _config.delivery is DeliveryMode.vendor_package:
            # One opaque package that the platform's own updater applies; otaclient
            # fetches it and the DPI hands it over.
            assert _config.package is not None  # the schema refuses the pair
            return DownloadPlan(
                blobs=(
                    PlannedBlob(_config.package, "the vendor package"),
                    *self._planned_update_agents(),
                )
            )

        _blobs: List[PlannedBlob] = list(self._planned_update_agents())
        for _partition in _config.partitions:
            if _partition.action is not PartitionAction.write:
                continue  # mkfs and keep carry nothing to download
            assert (
                _partition.image is not None
            )  # the schema refuses a write without one
            if _partition.delta is None:
                _blobs.append(
                    PlannedBlob(_partition.image, f"the {_partition.name} image")
                )
            elif self._delta_fits(_partition):
                _blobs.append(
                    PlannedBlob(_partition.delta, f"the {_partition.name} delta")
                )
            else:
                raise ota_errors.ApplyOTAUpdateFailed(
                    f"the {_partition.name} delta does not apply to this device: it is "
                    "not at the version the delta was built from",
                    module=__name__,
                )
        # Data images, the same way: the DPI hashes the file it mounts for the name,
        # so a delta is planned only when the device holds its source.
        for _data in _config.data_images:
            _what = f"the {_data.name} data image"
            if _data.delta is None:
                _blobs.append(PlannedBlob(_data.image, _what))
            elif self._delta_fits(_data, data_image=_data.name):
                _blobs.append(PlannedBlob(_data.delta, f"{_what} delta"))
            else:
                raise ota_errors.ApplyOTAUpdateFailed(
                    f"{_what} delta does not apply to this device: it does not hold "
                    "the image the delta was built from",
                    module=__name__,
                )
        return DownloadPlan(blobs=tuple(_blobs))

    def _planned_update_agents(self) -> tuple[PlannedBlob, ...]:
        """Every agent bundle the image ships: which one to install is the DPI's
        choice, and they are megabytes beside a payload of gigabytes."""
        if self.update_agent_manifest is None:
            return ()
        return tuple(
            PlannedBlob(_b, f"the {_b.annotations.type} agent {_b.annotations.version}")
            for _b in self.update_agent_manifest.layers
        )

    def _delta_fits(self, partition, *, data_image: Optional[str] = None) -> bool:
        """Whether the committed slot (or the named data image's file) holds the bytes
        the delta names, as the DPI hashes them; a DPI that cannot answer is a
        refusal."""
        _delta = partition.delta
        try:
            _on_device = self._dpi.source_digest(
                size=_delta.annotations.source_size, data_image=data_image
            )
        except DPIError as e:
            logger.warning(
                f"cannot tell whether the {partition.name} delta applies: {e!r}"
            )
            return False

        _wanted = _delta.annotations.source_digest
        _wanted_hex = _wanted.split(":")[-1]
        if _on_device == _wanted_hex:
            logger.info(f"the {partition.name} delta applies to this device")
            return True
        logger.info(
            f"the {partition.name} delta applies to {_wanted_hex[:12]}… but this device "
            f"holds {_on_device[:12]}…"
        )
        return False

    # ------ fetching and applying ------ #

    def download_payload(self, plan: DownloadPlan) -> Path:
        self._check_there_is_room_for(plan)
        for _blob in plan.blobs:
            logger.info(f"downloading {_blob.what}: {_blob.size} bytes")
            try:
                self._fetch_blob(_blob.descriptor, what=_blob.what)
            except OSError as e:
                if e.errno != errno.ENOSPC:
                    raise
                # The check above passed and the disk filled anyway: something else
                # on this device is writing too. Still a space problem, and still one
                # that clearing space and retrying fixes.
                raise ota_errors.StandbySlotInsufficientSpace(
                    f"ran out of room staging {_blob.what} in {self.image_dir}",
                    module=__name__,
                ) from e
        return self.image_dir

    def _check_there_is_room_for(self, plan: DownloadPlan) -> None:
        """Refuse before downloading rather than partway through: the first blob is
        most of the payload, and a device with no room should be reported as one."""
        self.image_dir.mkdir(exist_ok=True, parents=True)
        _free = shutil.disk_usage(self.image_dir).free
        _have = sum(
            _b.size for _b in plan.blobs if (self._blob_dir / _b.digest_hex).is_file()
        )
        _needed = plan.total_size - _have
        if _needed > _free:
            raise ota_errors.StandbySlotInsufficientSpace(
                f"the payload needs {human_readable_size(_needed)} staged in "
                f"{self.image_dir}, which has {human_readable_size(_free)} free",
                module=__name__,
            )

    def apply(
        self,
        *,
        version: str,
        rollback: bool = False,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> bool:
        """Hand the unpacked image to the DPI.

        Returns:
            Whether a reboot is needed, as the DPI reported it.
        """
        try:
            return self._dpi.install(
                package=self.image_dir,
                version=version,
                rollback=rollback,
                on_progress=on_progress or self._on_progress,
                # An image built for a vehicle carries one payload per ECU and the DPI
                # refuses to choose between them; this is the same answer otaclient
                # used to pick the payload it downloaded, so the two cannot disagree.
                ecu_id=self._image_id.ecu_id,
                release_key=self._image_id.release_key.value,
            )
        except DPIError as e:
            raise ota_errors.ApplyOTAUpdateFailed(
                f"the DPI did not apply the payload: {e!r}", module=__name__
            ) from e


#
# ------ the updater otaclient runs ------ #
#


class OTAUpdaterForPartitionImage(OTAUpdateInitializer):
    """An OTA update that writes partition images, from otaclient's side.

    The phases are the ones every update reports — metadata, downloading, applying,
    post-update, finalizing — so the console and the metrics read the same as any
    other. What happens inside them is different only where the payload is: nothing
    is mounted, the delta is decided by asking the device, and the writing is the
    DPI's.
    """

    def __init__(
        self,
        *,
        boot_controller: PartitionImageBootController,
        abort_handler: AbortHandler,
        image_identifier: ImageIdentifier,
        staging_dir: Optional[Union[str, Path]] = None,
        dpi: Optional[DPIClient] = None,
        **kwargs: Unpack[OTAUpdateInterfaceArgs],
    ) -> None:
        super().__init__(**kwargs)
        self._boot_controller = boot_controller
        self._abort_handler = abort_handler
        self._image_id = image_identifier
        self._dpi = dpi if dpi is not None else dpi_from_ecu_info()
        if staging_dir is None:
            staging_dir = staging_dir_for(boot_controller.bootloader_type)
        self._staging_root = Path(staging_dir)
        self._written_bytes_reported = 0

    # ------ phases ------ #

    def _report_phase(self, phase: UpdatePhase) -> int:
        _now = int(time.time())
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=phase, trigger_timestamp=_now
                ),
                session_id=self.session_id,
            )
        )
        return _now

    def _on_write_progress(self, percent: int, *, total_bytes: int) -> None:
        """The DPI counts percent of a partition; otaclient counts bytes applied.

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

    def _prune_staging(self, keep: Path) -> None:
        """Last update's payload is dead weight on optdata; this one's may be a retry.

        Only what this update staged is kept. The blobs are named by digest, so a
        retry of the same version finds its downloads where it left them.
        """
        if not self._staging_root.is_dir():
            return
        for _entry in self._staging_root.iterdir():
            if _entry == keep or not _entry.is_dir():
                continue
            logger.info(f"removing the payload staged for an earlier update: {_entry}")
            shutil.rmtree(_entry, ignore_errors=True)

    # ------ the update ------ #

    def execute(self) -> None:
        logger.info(
            f"execute local partition-image update({ecu_info.ecu_id=}): "
            f"{self.update_version=}, {self.release_name=}, {self.release_id=}"
        )
        _session_staging = self._staging_root / self.update_version
        try:
            self._prune_staging(keep=_session_staging)
            _session_staging.mkdir(exist_ok=True, parents=True)

            _updater = PartitionImageUpdater(
                base_url=self.url_base,
                downloader=self._downloader_pool.get_instance(),
                session_dir=_session_staging,
                image_id=self._image_id,
                dpi=self._dpi,
            )

            # ------ metadata ------ #
            _now = self._report_phase(UpdatePhase.PROCESSING_METADATA)
            self._metrics.processing_metadata_start_timestamp = _now
            _config = _updater.download_metadata()

            # ------ what this device needs ------ #
            _plan = _updater.plan()
            # What the partitions end up holding: for a compressed blob that is in
            # its annotations, not the stored size.
            _total_write = sum(
                getattr(_p.image, "image_size", _p.image.size)
                for _p in _config.partitions
                if _p.action is PartitionAction.write and _p.image is not None
            )
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
                    ),
                    session_id=self.session_id,
                )
            )
            self._metrics.delta_download_files_num = len(_plan.blobs)
            self._metrics.delta_download_files_size = _plan.total_size

            # ------ downloading ------ #
            _now = self._report_phase(UpdatePhase.DOWNLOADING_OTA_FILES)
            self._metrics.download_start_timestamp = _now
            _updater.download_payload(_plan)
            for _blob in _plan.blobs:
                self._status_report_queue.put_nowait(
                    StatusReport(
                        payload=UpdateProgressReport(
                            operation=UpdateProgressReport.Type.DOWNLOAD_REMOTE_COPY,
                            processed_file_num=1,
                            processed_file_size=_blob.size,
                            downloaded_bytes=_blob.size,
                        ),
                        session_id=self.session_id,
                    )
                )

            # ------ writing ------ #
            # The critical zone is the write and what it arms: a payload half written
            # onto the standby slot is safe to abandon, but only after the record and
            # the boot switch agree with each other.
            # Every partition `keep` is a payload of data images alone: no slot is
            # written and the trial boot is of the running slot, which the status
            # files have to expect.
            _writes_slot = any(
                _p.action is not PartitionAction.keep for _p in _config.partitions
            )
            with self._abort_handler.critical_zone():
                self._boot_controller.pre_update(
                    standby_as_ref=False, erase_standby=True, writes_slot=_writes_slot
                )

                _now = self._report_phase(UpdatePhase.APPLYING_UPDATE)
                self._metrics.apply_update_start_timestamp = _now
                _reboot_required = _updater.apply(
                    version=_config.image_version,
                    on_progress=lambda _p: self._on_write_progress(
                        _p, total_bytes=_total_write
                    ),
                )

            self._abort_handler.enter_final_phase()

            # ------ post update ------ #
            _now = self._report_phase(UpdatePhase.PROCESSING_POSTUPDATE)
            self._metrics.post_update_start_timestamp = _now
            _version_detail: Optional[VersionDetail] = None
            if self.release_name and self.release_id and self.image_id:
                _version_detail = VersionDetail(
                    release_name=self.release_name,
                    release_id=self.release_id,
                    image_id=self.image_id,
                )
            self._boot_controller.post_update(
                self.update_version, version_detail=_version_detail
            )

            if not _reboot_required:
                logger.info("the payload needs no reboot; the update is done")
                return
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
                raise ota_errors.OTAAbortSignal(
                    "abort in progress", module=__name__
                ) from e
            self._boot_controller.on_operation_failure()
            raise ota_errors.ApplyOTAUpdateFailed(
                f"unspecific error, update failed: {e!r}", module=__name__
            ) from e
        finally:
            self._downloader_pool.release_instance()
            shutil.rmtree(self._session_workdir, ignore_errors=True)

    def _finalize(self, staging: Path) -> None:
        """Drop what was staged (the payload is on the standby slot now), then wait
        for the sub ECUs, publish the metrics and reboot into the trial slot."""
        shutil.rmtree(staging, ignore_errors=True)
        self._finalize_update_and_reboot(self._boot_controller)
