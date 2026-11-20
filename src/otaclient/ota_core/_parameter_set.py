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
"""An update that lays a parameter set over the running base: no standby slot, no
reboot (unless a slot's `apply` asks for one). See `otaclient.parameter_set`."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import threading
import time
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path

from ota_image_libs.common.oci_spec import Sha256Digest
from ota_image_libs.v1.image_manifest.schema import ImageIdentifier
from ota_image_libs.v1.parameter_set.layer import (
    ParameterLayerInfo,
    ParameterLayerInvalid,
    read_parameter_layer,
)
from ota_image_libs.v1.parameter_set.schema import (
    ParameterSetConfig,
    ParameterSetManifest,
)
from ota_image_libs.v1.parameter_set.slots import (
    APPLY_REBOOT,
    RESERVED_PATHS,
    apply_comes_with_set,
    check_applies_in_set,
    check_slots_in_tree,
    slot_of,
)
from typing_extensions import Unpack

from ota_metadata.errors import ImageMetadataInvalid
from ota_metadata.utils.cert_store import CAStoreMap
from otaclient import errors as ota_errors
from otaclient._types import UpdatePhase, VersionDetail
from otaclient.boot_control._image_version_file import running_image_id
from otaclient.boot_control._platform_paths import (
    APPLY_MAY_BE_IN,
    OTA_PLATFORM_PATHS,
    platform_reserved_paths,
)
from otaclient.boot_control.protocol import ParameterSetBootControllerProtocol
from otaclient.configs.cfg import cfg, ecu_info
from otaclient.ota_core._abort_handler import AbortHandler
from otaclient.ota_core._common import download_exception_handler
from otaclient.ota_core._update_libs import (
    metadata_download_err_handler,
    raise_on_download_resources_failed,
)
from otaclient.ota_core._updater_base import (
    WAIT_BEFORE_REBOOT,
    OTAUpdateInitializer,
    OTAUpdateInterfaceArgs,
)
from otaclient.parameter_set import (
    CONFIG_FNAME,
    MANIFEST_FNAME,
    TREE_DNAME,
    AppliedRecord,
    ParameterSetFailed,
    ParameterSetStore,
    RecordedSlot,
)
from otaclient_common import _env
from otaclient_common.download_info import DownloadInfo

logger = logging.getLogger(__name__)

LAYER_FNAME = "layer.tar.zst"


BOOT_UNIT = "otaclient-parameter-set.service"
"""The unit that lays the applied set at boot (`otaclient mount-parameter-set`)."""
BOOT_UNIT_WANTS = tuple(
    f"{_d}/local-fs.target.wants/{BOOT_UNIT}"
    for _d in ("/etc/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system")
)
"""Where the unit is enabled, in the image or by the platform."""


def reserved_paths(*, host_root: str = cfg.CANONICAL_ROOT) -> tuple[str, ...]:
    """What otaclient reads or writes for itself, besides what every consumer does
    (the image library's RESERVED_PATHS, as of 0.8.0: /boot, /etc/fstab, /opt/ota,
    /usr/share/ota): never a slot's, nor inside one.

    The parameter set store is among them (a slot over it would hide the set it is
    laid from), with what the image library reserves from its next release on
    (`OTA_PLATFORM_PATHS`: /opt/data/ota, which holds the store, /etc/ota and
    /usr/lib/ota) and what otaclient and the OTA platform keep outside the slots'
    own files (`platform_reserved_paths`, the DPI this ECU names as it resolves on
    the host at `host_root`): a slot over one of them would hide what the platform
    or the next update needs."""
    _paths = (
        "/proc",
        "/sys",
        "/dev",
        "/run",
        cfg.DYNAMIC_CLIENT_MNT_HOST_ROOT,
        cfg.DYNAMIC_CLIENT_SQUASHFS_FILE,
        cfg.OTAPROXY_CACHE_DIR,
        cfg.OTA_TMP_STORE,
        cfg.OTA_RESOURCES_STORE,
        cfg.OTA_META_STORE,
        cfg.OTA_DOWNLOAD_DIR,
        cfg.PARAMETER_SET_DPATH,
        *platform_reserved_paths(
            dpi_executable=ecu_info.dpi_executable, host_root=host_root
        ),
    )
    return tuple(dict.fromkeys(_paths))


def _within(path: str, top: str) -> bool:
    return path == top or path.startswith(top.rstrip("/") + "/")


class OTAUpdaterForParameterSet(OTAUpdateInitializer):
    """An OTA update whose payload for this ECU is a parameter set.

    The phases are the ones every update reports; the standby slot is not touched and
    the update ends without a reboot, at SUCCESS, with the set's release recorded as
    the current slot's. A set that cannot be put in place, or whose release cannot be
    recorded, leaves the slot with the set it had.

    An abort is taken until the set is about to be switched to (the staging is
    discarded); from there on it is refused, as at the reboot of any other update.

    The set names no base: it is laid over whatever image the ECU runs, as long as
    that image has room for its slots (a slot directory it lacks is made) and the
    applies the set does not carry itself. Which images a set suits is decided where
    the release is evaluated.
    """

    def __init__(
        self,
        *,
        ca_store: CAStoreMap,
        boot_controller: ParameterSetBootControllerProtocol,
        abort_handler: AbortHandler,
        image_identifier: ImageIdentifier,
        base_committed: bool,
        store: ParameterSetStore | None = None,
        host_root: Path | None = None,
        **kwargs: Unpack[OTAUpdateInterfaceArgs],
    ) -> None:
        super().__init__(**kwargs)
        self._boot_controller = boot_controller
        # whether the running base is committed: until it is, the platform may still
        #   fall back to the base a set in the store was applied on
        self._base_committed = base_committed
        self._abort_handler = abort_handler
        self._image_id = image_identifier
        self._store = store or ParameterSetStore()
        # where the running image's files are seen: what a slot's `apply` runs in
        self._host_root = host_root or Path(
            _env.get_dynamic_client_chroot_path() or cfg.CANONICAL_ROOT
        )
        self._ota_image_helper, self._download_helper = (
            self._create_ota_image_v1_helpers(ca_store=ca_store)
        )
        self.manifest: ParameterSetManifest | None = None
        self.config: ParameterSetConfig | None = None
        # the running image's ID, from its version file (`running_image_id`), read
        #   when the update starts
        self._running_image_id = ""
        self._set_digest: Sha256Digest | None = None

    def _download(
        self, batches: Generator[list[DownloadInfo]], cond: threading.Condition
    ) -> None:
        for _fut in self._download_helper.download_meta_files(batches, condition=cond):
            download_exception_handler(_fut)

    def _metadata(self, cond: threading.Condition) -> Generator[list[DownloadInfo]]:
        """The set's manifest and config, downloaded against the verified index.

        A manifest or config that does not parse or does not agree is an invalid
        image (ImageMetadataInvalid), as for any v1 payload."""
        _index = self._ota_image_helper.image_index
        assert _index is not None
        _descriptor = _index.find_parameter_set(self._image_id)
        if _descriptor is None:
            raise ImageMetadataInvalid(
                f"the OTA image carries no parameter set for {self._image_id}"
            )
        self._set_digest = _descriptor.digest
        _manifest_f = self._session_workdir / MANIFEST_FNAME
        with cond:
            yield [
                self._ota_image_helper.download_from_descriptor(
                    _manifest_f, _descriptor
                )
            ]
            cond.wait()
        try:
            self.manifest = ParameterSetManifest.parse_metafile(_manifest_f.read_text())
        except Exception as e:
            raise ImageMetadataInvalid(f"the set's manifest is invalid: {e}") from e

        _config_f = self._session_workdir / CONFIG_FNAME
        with cond:
            yield [
                self._ota_image_helper.download_from_descriptor(
                    _config_f, self.manifest.config
                )
            ]
            cond.wait()
        try:
            self.config = ParameterSetConfig.parse_metafile(_config_f.read_text())
            self.manifest.check_config(self.config)
        except Exception as e:
            raise ImageMetadataInvalid(f"the set's config is invalid: {e}") from e

    def _layer(
        self, cond: threading.Condition, dst: Path
    ) -> Generator[list[DownloadInfo]]:
        assert self.manifest is not None and self.manifest.layer is not None
        with cond:
            yield [
                self._ota_image_helper.download_from_descriptor(
                    dst, self.manifest.layer
                )
            ]
            cond.wait()

    def _check_fits_the_running_base(self) -> None:
        """The running image has room for each slot (a directory, or none: otaclient
        makes the mount point) and each `apply` of the base, and nothing but
        otaclient's own set is mounted on a slot: a set the image has no room for is
        refused before anything changes. An `apply` the set carries (inside its
        slots) is checked against its layer once unpacked.

        What the applied set covers is not the base's: a slot or an `apply` of the
        base inside a slot of the applied one (and not that slot itself) would be
        checked against the set before, and is refused too.
        """
        assert self.config is not None
        self._check_the_ecu_keeps_the_set()
        _reserved = reserved_paths(host_root=str(self._host_root))
        try:
            check_slots_in_tree(self._host_root, self.config.slots, reserved=_reserved)
        except ValueError as e:
            raise ota_errors.ParameterSetNotApplicable(
                f"the running image has no room for the parameter set: {e}",
                module=__name__,
            ) from e
        # the set of another base in a store the slots share was not laid here: it
        #   covers nothing of this base
        _applied = self._store.applied_record_of(
            self._running_image_id, slot_records=self._boot_controller
        )
        _covered = [_s.path for _s in _applied.slots] if _applied else []
        for _slot in self.config.slots:
            _of_base = [(_slot.path, "path")]
            if not apply_comes_with_set(_slot, self.config.slots):
                _of_base.append((_slot.apply, "apply"))
                self._check_apply_not_reserved(_slot.name, _slot.apply, _reserved)
            for _path, _what in _of_base:
                _over = next(
                    (_c for _c in _covered if _path.startswith(_c + "/")), None
                )
                if _over is not None:
                    raise ota_errors.ParameterSetNotApplicable(
                        f"slot {_slot.name!r}: its {_what} {_path} is inside {_over}, "
                        "a slot of the applied set, which hides the base's own there",
                        module=__name__,
                    )
            if _slot.path not in _covered and self._store.is_mounted(_slot.path):
                raise ota_errors.ParameterSetNotApplicable(
                    f"slot {_slot.name!r}: something else is mounted at {_slot.path}",
                    module=__name__,
                )
            self._check_mount_point_can_be_made(_slot.name, _slot.path)
        self._note_applies_others_can_change()

    def _check_the_ecu_keeps_the_set(self) -> None:
        """This ECU can keep the set it is given, and lay it at every boot; refused
        before anything is written otherwise:

        - the store is root's alone: nothing is stored where another user could plant
          a set whose `apply` runs as root;
        - the boot-time unit is enabled on the host: without it, a reboot lays the set
          only once otaclient starts (or never, with an otaclient that does not know
          it), while its release is reported all along;
        - no update staged in this boot may have armed the standby slot: a reboot,
          the one a set may ask for included, would boot that trial;
        - a store the slots share can tell the base a set is applied on: the running
          image has an ID;
        - the store holds no set of another base the platform may still fall back to:
          until the running base is committed, that set stays the other base's.
        """
        if (_why := self._store.why_not_trusted()) is not None:
            raise ota_errors.ParameterSetNotApplicable(
                f"the parameter set store {self._store.root} cannot be trusted: {_why}",
                module=__name__,
            )
        if not any(
            os.path.lexists(os.path.join(self._host_root, _w.lstrip("/")))
            for _w in BOOT_UNIT_WANTS
        ):
            raise ota_errors.ParameterSetNotApplicable(
                f"{BOOT_UNIT}, which lays the parameter set at boot, is not enabled "
                f"on this ECU (none of {list(BOOT_UNIT_WANTS)})",
                module=__name__,
            )
        if self._boot_controller.standby_armed_in_this_boot():
            raise ota_errors.ParameterSetNotApplicable(
                "an update was staged in this boot and may have armed the standby "
                "slot for a trial boot: apply the parameter set after the next reboot",
                module=__name__,
            )
        if not self._running_image_id and self._store.is_shared():
            raise ota_errors.ParameterSetNotApplicable(
                f"the running image has no image ID in {cfg.IMAGE_VERSION_FILE_FPATH}, "
                "which a store the slots share needs to tell the base a set was "
                "applied on",
                module=__name__,
            )
        _, _why = self._store.whose_set(
            self._running_image_id, slot_records=self._boot_controller
        )
        # a store on the slot's own root holds no set of a base to fall back to
        if _why is not None and not self._base_committed and self._store.is_shared():
            raise ota_errors.ParameterSetNotApplicable(
                "the store holds the set of the base this ECU may still fall back to "
                f"({_why}): apply again once this base is committed",
                module=__name__,
            )

    def _check_apply_not_reserved(
        self, name: str, apply: str, reserved: tuple[str, ...]
    ) -> None:
        """An `apply` of the base is not in a path otaclient or the platform uses for
        itself: what runs there is theirs, not a set's.

        TODO(ota-image-libs>0.8.0): refused by `check_slots_in_tree` from its next
        release on (with /opt/data/ota, /etc/ota and /usr/lib/ota among its
        RESERVED_PATHS)."""
        for _top in (*RESERVED_PATHS, *OTA_PLATFORM_PATHS, *reserved):
            if _top in APPLY_MAY_BE_IN:
                continue
            if _within(apply, _top):
                raise ota_errors.ParameterSetNotApplicable(
                    f"slot {name!r}: its apply {apply} is in {_top}, which otaclient "
                    "or the OTA platform uses for itself",
                    module=__name__,
                )

    def _check_mount_point_can_be_made(self, name: str, path: str) -> None:
        """A slot directory the running image does not have is made when the slot is
        laid: refused here, before anything changes, when where it goes cannot be
        written (a read-only filesystem, say: on the read-only root of a
        partition-based layout, the image must have each slot's directory)."""
        _root = os.path.realpath(self._host_root)
        _at = os.path.join(_root, path.lstrip("/"))
        if os.path.isdir(_at):
            return
        while _at != _root and not os.path.lexists(_at):
            _at = os.path.dirname(_at)
        if not os.access(_at, os.W_OK) or os.statvfs(_at).f_flag & os.ST_RDONLY:
            raise ota_errors.ParameterSetNotApplicable(
                f"slot {name!r}: the running image has no {path}, and it cannot be "
                f"made: /{os.path.relpath(_at, _root)} is not writable",
                module=__name__,
            )

    def _note_applies_others_can_change(self) -> None:
        """An `apply` runs as root: one that users other than root can change (the
        file, or a directory on its way, writable by them or not root's) is logged and
        recorded. For an apply the set carries, the file and the slot holding it are
        the set's (root's, laid read-only, checked with the layer): what is looked at
        is the image's way to that slot, which a directory renamed there could
        redirect. Not refused: images in use have such directories (an application's
        tree owned by the application's user)."""
        assert self.config is not None
        _root = os.path.realpath(self._host_root)
        _ways: dict[str, str] = {}
        for _slot in self.config.slots:
            if _slot.apply == APPLY_REBOOT:
                continue
            _in = (
                slot_of(_slot.apply[1:], self.config.slots)
                if apply_comes_with_set(_slot, self.config.slots)
                else None
            )
            # the way to look at: the apply itself, or what holds the slot it is in
            _ways.setdefault(
                _slot.apply, os.path.dirname(_in.path) if _in else _slot.apply
            )
        for _apply, _start in _ways.items():
            _writable: list[str] = []
            _path = _start
            while True:
                try:
                    _st = os.lstat(os.path.join(_root, _path.lstrip("/")))
                except FileNotFoundError:
                    _st = None  # made by otaclient as the mount point: root's
                if _st is not None and (
                    _st.st_uid != 0 or _st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                ):
                    _writable.append(_path)
                if _path == "/":
                    break
                _path = os.path.dirname(_path)
            if _writable:
                logger.warning(
                    f"apply {_apply} runs as root, but users other than root can "
                    f"change it through {_writable}"
                )
                self._store.record_event(
                    "apply_not_root_only",
                    apply=_apply,
                    writable=_writable,
                    session_id=self.session_id,
                )

    def _unpack(self, staging: Path, layer_f: Path) -> ParameterLayerInfo:
        """Unpack and check the layer into the staging; the files the config lists,
        when it lists them, are exactly those, and an `apply` inside the set's slots
        is among the unpacked files as an executable regular file others cannot
        write."""
        assert self.manifest is not None and self.config is not None
        if self.manifest.layer is None:
            return ParameterLayerInfo()
        _tree = staging / TREE_DNAME
        _tree.mkdir(mode=0o755)
        try:
            with open(layer_f, "rb") as _blob:
                _info = read_parameter_layer(
                    _blob,
                    self.config.slots,
                    extract_to=_tree,
                    max_size=cfg.PARAMETER_SET_MAX_SIZE,
                    blob_digest=self.manifest.layer.digest,
                    diff_id=self.config.diff_ids[0],
                )
        except ParameterLayerInvalid as e:
            raise ota_errors.OTAImageInvalid(
                f"the parameter set's layer is invalid: {e}", module=__name__
            ) from e
        try:
            self.config.check_files(_info.files)
        except ValueError as e:
            raise ota_errors.OTAImageInvalid(
                f"the parameter set's config does not agree with its layer: {e}",
                module=__name__,
            ) from e
        try:
            check_applies_in_set(_tree, self.config.slots)
        except ValueError as e:
            raise ota_errors.OTAImageInvalid(
                f"the apply the parameter set carries is not one to run: {e}",
                module=__name__,
            ) from e
        return _info

    def _record(self) -> AppliedRecord:
        """What the set is, and what it is applied on: the base's own records are
        carried from the set before, or taken from the slot for the first set."""
        assert self.manifest is not None and self.config is not None
        _before = self._store.applied_record_of(
            self._running_image_id, slot_records=self._boot_controller
        )
        if _before is not None:
            _base_version, _base_detail = (
                _before.base_version,
                _before.base_version_detail,
            )
        else:
            _base_version, _base_detail = self._boot_controller.load_recorded_version()
        return AppliedRecord(
            set_manifest_digest=str(self._set_digest),
            set_name=self.config.name,
            set_version=self.config.version,
            slots=[RecordedSlot.of(_s) for _s in self.config.slots],
            base_image_id=self._running_image_id,
            firmware_version=self.update_version,
            release_name=self.release_name,
            release_id=self.release_id,
            image_id=self.image_id,
            base_version=_base_version,
            base_version_detail=_base_detail,
            applied_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            session_id=self.session_id,
        )

    def _mark_in_progress(self) -> None:
        _err = ota_errors.ParameterSetApplyFailed(
            "the parameter set update did not finish: otaclient stopped during it",
            module=__name__,
        )
        self._boot_controller.store_failure_info(
            failure_type=_err.failure_type, failure_reason=_err.get_failure_reason()
        )

    @staticmethod
    def _apply_failed(detail: str, *, restored: bool) -> ota_errors.OTAError:
        """Back to what the ECU had before the update: it can be retried; else what
        the ECU runs with is not known."""
        if restored:
            return ota_errors.ParameterSetApplyFailed(detail, module=__name__)
        return ota_errors.ParameterSetRestoreFailed(detail, module=__name__)

    def _restore_version(self, version: tuple[str, VersionDetail | None]) -> bool:
        try:
            self._boot_controller.restore_recorded_version(
                version=version[0], version_detail=version[1]
            )
            return True
        except Exception as e:
            logger.error(f"failed to put the slot's records back: {e!r}")
            self._store.record_event("records_not_restored", error=repr(e))
            return False

    def execute(self) -> bool:
        """Lay the set in place. Returns True when a slot's `apply` asks for a reboot,
        which the caller does once the status is reported."""
        logger.info(
            f"execute parameter set update ({self._image_id}): "
            f"{self.update_version=}, {self.release_name=}, {self.release_id=}"
        )
        _cond = threading.Condition()
        # whether this update made the staging, which then goes at its end
        _staged = False
        try:
            # ------ metadata ------ #
            _now = self._report_phase(UpdatePhase.PROCESSING_METADATA)
            self._metrics.processing_metadata_start_timestamp = _now
            with metadata_download_err_handler():
                self._download(
                    self._ota_image_helper.download_and_verify_image_index(
                        condition=_cond
                    ),
                    _cond,
                )
                self._download(self._metadata(_cond), _cond)
            assert self.manifest is not None and self.config is not None
            self._running_image_id = running_image_id()
            self._check_fits_the_running_base()
            logger.info(
                f"parameter set {self.config.name} {self.config.version}: slots "
                f"{[_s.name for _s in self.config.slots]}"
            )
            # until the set is in place and recorded, the slot reports a failure: an
            #   update cut short (otaclient stopped, power lost) is not taken for the
            #   success of the release before
            self._mark_in_progress()

            # ------ downloading ------ #
            _now = self._report_phase(UpdatePhase.DOWNLOADING_OTA_FILES)
            self._metrics.download_start_timestamp = _now
            _staging = self._store.prepare_staging()
            _staged = True
            _layer_f = self._session_workdir / LAYER_FNAME
            if self.manifest.layer is not None:
                try:
                    self._download(self._layer(_cond, _layer_f), _cond)
                except Exception as e:
                    raise_on_download_resources_failed(e)

            # ------ applying ------ #
            # the staging is the set's own: an abort until here only discards it
            _now = self._report_phase(UpdatePhase.APPLYING_UPDATE)
            self._metrics.apply_update_start_timestamp = _now
            _info = self._unpack(_staging, _layer_f)
            for _f in (MANIFEST_FNAME, CONFIG_FNAME):
                shutil.copyfile(self._session_workdir / _f, _staging / _f)
            self._store.stage(self._record(), _info.files)

            # what the slot reports now: put back if the new release is not recorded
            _version_before = self._boot_controller.load_recorded_version()

            # from the switch on, the slot changes: no abort, as at a reboot
            self._abort_handler.enter_final_phase()
            # a set of another base the store holds (this base committed: checked
            #   before) is replaced, not the set before
            self._store.set_aside_another_bases_set(
                self._running_image_id, slot_records=self._boot_controller
            )
            try:
                _reboot = self._store.switch()
            except ParameterSetFailed as e:
                raise self._apply_failed(str(e), restored=e.restored) from e

            # ------ post update ------ #
            _now = self._report_phase(UpdatePhase.PROCESSING_POSTUPDATE)
            self._metrics.post_update_start_timestamp = _now
            try:
                self._boot_controller.on_parameter_set_applied(
                    version=self.update_version, version_detail=self._version_detail
                )
            except Exception as e:
                logger.error(f"failed to record the release: {e!r}; rolling back")
                _restored = self._store.roll_back(cause=e)
                _restored = self._restore_version(_version_before) and _restored
                raise self._apply_failed(
                    f"the release could not be recorded: {e!r}", restored=_restored
                ) from e
            self._store.commit()
            return _reboot

        except ota_errors.OTAAbortSignal:
            logger.info("parameter set update aborted")
            raise
        except Exception as e:
            # an error not raised above came before the switch: nothing changed
            _err = (
                e
                if isinstance(e, ota_errors.OTAError)
                else ota_errors.ParameterSetApplyFailed(
                    f"unspecific error, nothing changed: {e!r}", module=__name__
                )
            )
            self._store.record_event(
                "update_failed", session_id=self.session_id, error=str(_err)
            )
            if _err is e:
                raise
            raise _err from e
        finally:
            if _staged:
                self._store.discard_staging()
            self._downloader_pool.release_instance()
            self._downloader_pool.shutdown()
            shutil.rmtree(self._session_workdir, ignore_errors=True)

    def reboot_for_apply(self) -> None:
        """A changed slot's `apply` is `reboot`: once the status is reported, wait for
        the sub ECUs (this ECU may be their proxy), publish the metrics and reboot the
        same slot, the way the boot controller reboots on this layout."""
        self._wait_for_sub_ecus_and_publish_metrics()
        logger.info(f"a slot asks for a reboot: rebooting in {WAIT_BEFORE_REBOOT}s")
        time.sleep(WAIT_BEFORE_REBOOT)
        self._boot_controller.reboot_current_slot()
