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
import shutil
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
    check_against_base,
)
from ota_image_libs.v1.parameter_set.slots import ParameterSlot
from typing_extensions import Unpack

from ota_metadata.errors import ImageMetadataInvalid
from ota_metadata.utils.cert_store import CAStoreMap
from otaclient import errors as ota_errors
from otaclient._types import UpdatePhase
from otaclient.boot_control._base import BootControllerBase
from otaclient.boot_control._image_version_file import load_image_version_detail
from otaclient.configs.cfg import cfg
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
    RunningBase,
    load_running_base,
)
from otaclient_common import _env, cmdhelper
from otaclient_common.download_info import DownloadInfo

logger = logging.getLogger(__name__)

LAYER_FNAME = "layer.tar.zst"


class OTAUpdaterForParameterSet(OTAUpdateInitializer):
    """An OTA update whose payload for this ECU is a parameter set.

    The phases are the ones every update reports; the standby slot is not touched and
    the update ends without a reboot, at SUCCESS, with the set's release recorded as
    the current slot's. A set that cannot be put in place, or whose release cannot be
    recorded, leaves the slot with the set it had.

    An abort is taken until the set is about to be switched to (the staging is
    discarded); from there on it is refused, as at the reboot of any other update.
    """

    def __init__(
        self,
        *,
        ca_store: CAStoreMap,
        boot_controller: BootControllerBase,
        abort_handler: AbortHandler,
        image_identifier: ImageIdentifier,
        store: ParameterSetStore | None = None,
        image_meta_dir: Path | None = None,
        **kwargs: Unpack[OTAUpdateInterfaceArgs],
    ) -> None:
        super().__init__(**kwargs)
        self._boot_controller = boot_controller
        self._abort_handler = abort_handler
        self._image_id = image_identifier
        self._store = store or ParameterSetStore()
        self._image_meta_dir = image_meta_dir or Path(cfg.IMAGE_META_DPATH)
        self._ota_image_helper, self._download_helper = (
            self._create_ota_image_v1_helpers(ca_store=ca_store)
        )
        self.manifest: ParameterSetManifest | None = None
        self.config: ParameterSetConfig | None = None
        self._set_digest: Sha256Digest | None = None
        self._base_digest = ""

    def _download(self, batches: Generator[list[DownloadInfo]], cond) -> None:
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

    def _check_against_the_running_base(
        self,
    ) -> tuple[RunningBase, list[ParameterSlot]]:
        assert self.manifest is not None and self.config is not None
        _base = load_running_base(self._image_meta_dir)
        if _base is None:
            raise ota_errors.OTAImageInvalid(
                "the running base was installed without its manifest kept, so no "
                "parameter set can be checked against it: a full OTA comes first",
                module=__name__,
            )
        try:
            _filled = check_against_base(
                self.manifest,
                self.config,
                base_manifest_digest=_base.manifest_digest,
                base_slots=_base.slots,
            )
        except ValueError as e:
            raise ota_errors.OTAImageInvalid(
                f"the parameter set does not fit the running base: {e}",
                module=__name__,
            ) from e
        return _base, _filled

    def _unpack(self, staging: Path, layer_f: Path) -> ParameterLayerInfo:
        """Unpack and check the layer into the staging; the files the config lists,
        when it lists them, are exactly those."""
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
        if self.config.files and sorted(
            (_f.path, _f.size, str(_f.digest)) for _f in self.config.files
        ) != sorted((_f.path, _f.size, str(_f.digest)) for _f in _info.files):
            raise ota_errors.OTAImageInvalid(
                "the parameter set's config lists other files than its layer holds",
                module=__name__,
            )
        return _info

    def _record(self, filled: list[ParameterSlot]) -> AppliedRecord:
        """What the set is, and what it is applied on: the base's own records are
        carried from the set before, or taken from the slot for the first set."""
        assert self.manifest is not None and self.config is not None
        _before = self._store.applied_record()
        if _before is not None:
            _base_version, _base_detail = (
                _before.base_version,
                _before.base_version_detail,
            )
        else:
            _base_version, _base_detail = self._boot_controller.load_recorded_version()
        _from_image = load_image_version_detail()
        return AppliedRecord(
            set_manifest_digest=str(self._set_digest),
            set_name=self.config.name,
            set_version=self.config.version,
            slots=filled,
            base_manifest_digest=self._base_digest,
            base_image_id=_from_image.image_id if _from_image else "",
            firmware_version=self.update_version,
            release_name=self.release_name,
            release_id=self.release_id,
            image_id=self.image_id,
            base_version=_base_version,
            base_version_detail=_base_detail,
            applied_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            session_id=self.session_id,
        )

    def execute(self) -> bool:
        """Lay the set in place. Returns True when a slot's `apply` asks for a reboot,
        which the caller does once the status is reported."""
        logger.info(
            f"execute parameter set update ({self._image_id}): "
            f"{self.update_version=}, {self.release_name=}, {self.release_id=}"
        )
        _cond = threading.Condition()
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
            _base, _filled = self._check_against_the_running_base()
            self._base_digest = str(_base.manifest_digest)
            logger.info(
                f"parameter set {self.config.name} {self.config.version}: slots "
                f"{[_s.name for _s in _filled]} over base {_base.manifest_digest}"
            )

            # ------ downloading ------ #
            _now = self._report_phase(UpdatePhase.DOWNLOADING_OTA_FILES)
            self._metrics.download_start_timestamp = _now
            _staging = self._store.prepare_staging()
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
            self._store.stage(self._record(_filled), _info.files)

            # from the switch on, the slot changes: no abort, as at a reboot
            self._abort_handler.enter_final_phase()
            _reboot = self._store.switch(base_slots=_base.slots)

            # ------ post update ------ #
            _now = self._report_phase(UpdatePhase.PROCESSING_POSTUPDATE)
            self._metrics.post_update_start_timestamp = _now
            try:
                with ota_errors.raise_as_ota_error(
                    ota_errors.PostUpdateFailed, "post-update failed", module=__name__
                ):
                    self._boot_controller.on_parameter_set_applied(
                        version=self.update_version, version_detail=self._version_detail
                    )
            except ota_errors.OTAError as e:
                # the slot still records the release before: so does the store
                self._store.roll_back(base_slots=_base.slots, cause=e)
                raise
            self._store.commit()
            return _reboot

        except ota_errors.OTAAbortSignal:
            logger.info("parameter set update aborted")
            raise
        except Exception as e:
            _err = (
                e
                if isinstance(e, ota_errors.OTAError)
                else ota_errors.ApplyOTAUpdateFailed(
                    str(e)
                    if isinstance(e, ParameterSetFailed)
                    else f"unspecific error, update failed: {e!r}",
                    module=__name__,
                )
            )
            self._store.record_event(
                "update_failed", session_id=self.session_id, error=str(_err)
            )
            if _err is e:
                raise
            raise _err from e
        finally:
            self._store.discard_staging()
            self._downloader_pool.release_instance()
            self._downloader_pool.shutdown()
            shutil.rmtree(self._session_workdir, ignore_errors=True)

    def reboot_for_apply(self) -> None:
        """A changed slot's `apply` is `reboot`: once the status is reported, wait for
        the sub ECUs (this ECU may be their proxy), publish the metrics and reboot the
        same slot."""
        self._wait_for_sub_ecus_and_publish_metrics()
        logger.info(f"a slot asks for a reboot: rebooting in {WAIT_BEFORE_REBOOT}s")
        time.sleep(WAIT_BEFORE_REBOOT)
        cmdhelper.reboot(chroot=_env.get_dynamic_client_chroot_path())
