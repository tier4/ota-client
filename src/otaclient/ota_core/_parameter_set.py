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

from ota_metadata.utils.cert_store import CAStoreMap
from otaclient import errors as ota_errors
from otaclient._types import UpdatePhase
from otaclient._utils import wait_and_log
from otaclient.boot_control._base import BootControllerBase
from otaclient.boot_control._image_version_file import load_image_version_detail
from otaclient.configs.cfg import cfg, proxy_info
from otaclient.ota_core._abort_handler import AbortHandler
from otaclient.ota_core._common import download_exception_handler
from otaclient.ota_core._update_libs import metadata_download_err_handler
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
    the current slot's. A set that cannot be put in place leaves the slot with the set
    it had.
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
        self._filled: list[ParameterSlot] = []
        self._base_digest = ""

    def _download(self, batches: Generator[list[DownloadInfo]], cond) -> None:
        for _fut in self._download_helper.download_meta_files(batches, condition=cond):
            download_exception_handler(_fut)

    def _metadata(self, cond: threading.Condition) -> Generator[list[DownloadInfo]]:
        """The set's manifest and config, downloaded against the verified index."""
        _index = self._ota_image_helper.image_index
        assert _index is not None
        _descriptor = _index.find_parameter_set(self._image_id)
        if _descriptor is None:
            raise ota_errors.OTAImageInvalid(
                f"the OTA image carries no parameter set for {self._image_id}",
                module=__name__,
            )
        _manifest_f = self._session_workdir / MANIFEST_FNAME
        with cond:
            yield [
                self._ota_image_helper.download_from_descriptor(
                    _manifest_f, _descriptor
                )
            ]
            cond.wait()
        self.manifest = ParameterSetManifest.parse_metafile(_manifest_f.read_text())

        _config_f = self._session_workdir / CONFIG_FNAME
        with cond:
            yield [
                self._ota_image_helper.download_from_descriptor(
                    _config_f, self.manifest.config
                )
            ]
            cond.wait()
        self.config = ParameterSetConfig.parse_metafile(_config_f.read_text())
        self.manifest.check_config(self.config)

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

    def _record(self, set_digest: Sha256Digest) -> AppliedRecord:
        """What the set is, and what it is applied on: the base's own records are
        carried from the set before, or taken from the slot for the first set."""
        assert self.manifest is not None and self.config is not None
        _before = self._store.applied_record()
        if _before is not None:
            _base_version = _before.base_version
            _base_detail = _before.base_version_detail
        else:
            _version, _detail = self._boot_controller.load_recorded_version()
            _base_version = _version
            _base_detail = (
                {
                    "release_name": _detail.release_name,
                    "release_id": _detail.release_id,
                    "image_id": _detail.image_id,
                }
                if _detail
                else None
            )
        _from_image = load_image_version_detail()
        return AppliedRecord(
            set_manifest_digest=str(set_digest),
            name=self.config.name,
            version=self.config.version,
            slots=self._filled,
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
        which the caller does once the status is recorded."""
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
            _descriptor = self._ota_image_helper.image_index.find_parameter_set(  # type: ignore[union-attr]
                self._image_id
            )
            assert _descriptor is not None

            _base = load_running_base(self._image_meta_dir)
            if _base is None:
                raise ota_errors.OTAImageInvalid(
                    "the running base was installed without its manifest kept, so no "
                    "parameter set can be checked against it: a full OTA comes first",
                    module=__name__,
                )
            try:
                self._filled = check_against_base(
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
            self._base_digest = str(_base.manifest_digest)
            logger.info(
                f"parameter set {self.config.name} {self.config.version}: slots "
                f"{[_s.name for _s in self._filled]} over base {_base.manifest_digest}"
            )

            # ------ downloading ------ #
            _now = self._report_phase(UpdatePhase.DOWNLOADING_OTA_FILES)
            self._metrics.download_start_timestamp = _now
            _staging = self._store.prepare_staging()
            _layer_f = self._session_workdir / LAYER_FNAME
            if self.manifest.layer is not None:
                self._download(self._layer(_cond, _layer_f), _cond)

            # ------ applying ------ #
            with self._abort_handler.critical_zone():
                _now = self._report_phase(UpdatePhase.APPLYING_UPDATE)
                self._metrics.apply_update_start_timestamp = _now
                # an empty set has no tree: every slot gets the base's defaults, and
                # its record still says which release this slot runs
                if self.manifest.layer is not None:
                    _tree = _staging / TREE_DNAME
                    _tree.mkdir(mode=0o755)
                    with open(_layer_f, "rb") as _blob:
                        read_parameter_layer(
                            _blob,
                            self.config.slots,
                            extract_to=_tree,
                            max_size=cfg.PARAMETER_SET_MAX_SIZE,
                            blob_digest=self.manifest.layer.digest,
                            diff_id=self.config.diff_ids[0],
                        )
                for _f in (MANIFEST_FNAME, CONFIG_FNAME):
                    shutil.copyfile(self._session_workdir / _f, _staging / _f)
                _reboot = self._store.switch(
                    new=self._record(_descriptor.digest), base_slots=_base.slots
                )

                # ------ post update ------ #
                _now = self._report_phase(UpdatePhase.PROCESSING_POSTUPDATE)
                self._metrics.post_update_start_timestamp = _now
                with ota_errors.raise_as_ota_error(
                    ota_errors.PostUpdateFailed, "post-update failed", module=__name__
                ):
                    self._boot_controller.on_parameter_set_applied(
                        version=self.update_version, version_detail=self._version_detail
                    )
            return _reboot

        except ota_errors.OTAError as e:
            self._store.history(
                "update_failed", session_id=self.session_id, error=str(e)
            )
            raise
        except ParameterLayerInvalid as e:
            self._store.history(
                "update_failed", session_id=self.session_id, error=str(e)
            )
            raise ota_errors.OTAImageInvalid(
                f"the parameter set's layer is invalid: {e}", module=__name__
            ) from e
        except ParameterSetFailed as e:
            self._store.history(
                "update_failed", session_id=self.session_id, error=str(e)
            )
            raise ota_errors.ApplyOTAUpdateFailed(str(e), module=__name__) from e
        except Exception as e:
            self._store.history(
                "update_failed", session_id=self.session_id, error=repr(e)
            )
            raise ota_errors.ApplyOTAUpdateFailed(
                f"unspecific error, update failed: {e!r}", module=__name__
            ) from e
        finally:
            self._store.discard_staging()
            self._downloader_pool.release_instance()
            self._downloader_pool.shutdown()
            shutil.rmtree(self._session_workdir, ignore_errors=True)

    def reboot_for_apply(self) -> None:
        """A slot's `apply` is `reboot`: once the status is recorded, wait for the sub
        ECUs (this ECU may be their proxy), publish the metrics and reboot the same
        slot."""
        if proxy_info.enable_local_ota_proxy:
            wait_and_log(
                check_flag=self.ecu_status_flags.any_child_ecu_in_update.is_set,
                check_for=False,
                message="permit reboot flag",
                log_func=logger.info,
            )
        self._metrics.reboot_start_timestamp = int(time.time())
        self._metrics.publish()
        logger.info(f"a slot asks for a reboot: rebooting in {WAIT_BEFORE_REBOOT}s")
        time.sleep(WAIT_BEFORE_REBOOT)
        cmdhelper.reboot(chroot=_env.get_dynamic_client_chroot_path())
