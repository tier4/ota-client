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
"""The parameter set update, from the index to the slot's records."""

from __future__ import annotations

import io
import json
import shutil
import threading
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from queue import Queue

import pytest
from ota_image_libs.common.oci_spec import Sha256Digest
from ota_image_libs.v1.annotation_keys import (
    BUILD_TOOL_VERSION,
    OTA_RELEASE_KEY,
    PARAMETER_SET_VERSION,
    PLATFORM_ECU,
)
from ota_image_libs.v1.image_index.schema import ImageIndex
from ota_image_libs.v1.image_manifest.schema import ImageIdentifier, OTAReleaseKey
from ota_image_libs.v1.parameter_set.layer import write_parameter_layer
from ota_image_libs.v1.parameter_set.schema import (
    EmptyDescriptor,
    FileBasedSubjectDescriptor,
    ParameterLayerDescriptor,
    ParameterSetConfig,
    ParameterSetFileEntry,
    ParameterSetManifest,
)
from ota_image_libs.v1.parameter_set.slots import ParameterSlot, ParameterSlotRef
from ota_image_libs.v1.update_agent_package.schema import EMPTY_JSON
from pytest_mock import MockerFixture

from ota_metadata.utils.cert_store import CAStoreMap
from otaclient import errors as ota_errors
from otaclient._status_monitor import OTAUpdatePhaseChangeReport
from otaclient._types import MultipleECUStatusFlags, UpdatePhase, VersionDetail
from otaclient.boot_control._base import BootControllerBase
from otaclient.metrics import OTAMetricsData
from otaclient.ota_core import _parameter_set
from otaclient.ota_core._parameter_set import OTAUpdaterForParameterSet
from otaclient.parameter_set import TREE_DNAME, ParameterSetStore, load_running_base

PARAMS = ParameterSlot(
    name="individual_params",
    path="/opt/autoware/vehicle/vehicle.j6_gen2/individual_params",
    apply="/opt/autoware/bin/apply-parameter-set",
)
BASE_MANIFEST = b'{"the": "base payload manifest"}'
BASE_DIGEST = Sha256Digest(sha256(BASE_MANIFEST).hexdigest())
THIS_ECU = ImageIdentifier("autoware", OTAReleaseKey.dev)
ANNOTATIONS = {
    PLATFORM_ECU: "autoware",
    OTA_RELEASE_KEY: "dev",
    PARAMETER_SET_VERSION: "2026.10.29-1",
}
IMAGE_CONFIG = {
    "schemaVersion": 1,
    "mediaType": "application/vnd.tier4.ota.file-based-ota-image.config.v1+json",
    "architecture": "x86_64",
    "file_table": {
        "mediaType": "application/vnd.tier4.ota.file-based-ota-image.file_table.v1.sqlite3+zstd",
        "digest": f"sha256:{'2' * 64}",
        "size": 1,
    },
    "labels": {
        "vnd.tier4.image.base-image": "ubuntu:22.04",
        "vnd.tier4.ota.image.blobs-count": 1,
        "vnd.tier4.ota.image.blobs-size": 1,
        "vnd.tier4.image.rootfs.regular-files-count": 1,
        "vnd.tier4.image.rootfs.non-regular-files-count": 0,
        "vnd.tier4.image.rootfs.dirs-count": 1,
        "vnd.tier4.image.rootfs.unique-files-entries-count": 1,
        "vnd.tier4.image.rootfs.unique-files-entries-size": 1,
    },
}


def parameter_image(
    root: Path,
    *,
    tree: Path | None,
    base: Sha256Digest = BASE_DIGEST,
    files: list | None = None,
    ecu: str = "autoware",
) -> tuple[Path, ImageIndex]:
    """The blobs of a parameter image with one set for THIS_ECU, and its index."""
    _blobs = root / "blobs"
    _blobs.mkdir(parents=True)
    if tree is None:
        _layer = EmptyDescriptor.add_contents_to_resource_dir(EMPTY_JSON, _blobs)
        _config = ParameterSetConfig(name="p", version="2026.10.29-1")
    else:
        _slots = [ParameterSlotRef(name=PARAMS.name, path=PARAMS.path)]
        _out = io.BytesIO()
        _info = write_parameter_layer(tree, _slots, _out)
        _layer = ParameterLayerDescriptor.add_contents_to_resource_dir(
            _out.getvalue(), _blobs
        )
        _config = ParameterSetConfig(
            name="p",
            version="2026.10.29-1",
            slots=_slots,
            diff_ids=[_info.diff_id],
            files=_info.files if files is None else files,
        )
    _manifest = ParameterSetManifest(
        config=ParameterSetConfig.Descriptor.export_metafile_to_resource_dir(
            _config, _blobs
        ),
        layers=[_layer],
        subject=FileBasedSubjectDescriptor(size=len(BASE_MANIFEST), digest=base),
        annotations=ParameterSetManifest.Annotations.model_validate(
            {**ANNOTATIONS, PLATFORM_ECU: ecu}
        ),
    )
    _index = ImageIndex(
        manifests=[],
        annotations=ImageIndex.Annotations.model_validate({BUILD_TOOL_VERSION: "t"}),
    )
    _index.add_image(
        ParameterSetManifest.Descriptor.export_metafile_to_resource_dir(
            _manifest, _blobs, annotations={**ANNOTATIONS, PLATFORM_ECU: ecu}
        )
    )
    return _blobs, _index


def set_tree(root: Path, content: str) -> Path:
    _f = root / PARAMS.relpath / "default" / "vehicle.param.yaml"
    _f.parent.mkdir(parents=True)
    _f.write_text(content)
    return root


class FakeMounter:
    def __init__(self) -> None:
        self.mounted: dict[str, Path] = {}

    def is_mounted(self, mount_point: str) -> bool:
        return mount_point in self.mounted

    def bind_ro(self, source: Path, mount_point: str) -> None:
        self.mounted[mount_point] = source

    def umount(self, mount_point: str) -> None:
        del self.mounted[mount_point]


class FakeRunner:
    def __init__(self) -> None:
        self.ran: list[str] = []
        self.fail_times = 0

    def __call__(self, command: str) -> None:
        self.ran.append(command)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError(f"{command} failed")


class FakeAbortHandler:
    state = None

    def __init__(self, *, abort_at_final_phase: bool = False) -> None:
        self._abort = abort_at_final_phase
        self.final_phase = False

    @contextmanager
    def critical_zone(self):
        yield

    def enter_final_phase(self) -> None:
        if self._abort:
            raise ota_errors.OTAAbortSignal("abort in progress", module=__name__)
        self.final_phase = True


@pytest.fixture
def image_meta(tmp_path: Path) -> Path:
    """What a full OTA of the base kept: its manifest and config, with the slot."""
    _dir = tmp_path / "image-meta"
    _dir.mkdir()
    (_dir / "image_manifest.json").write_bytes(BASE_MANIFEST)
    (_dir / "image_config.json").write_text(
        json.dumps({**IMAGE_CONFIG, "parameter_slots": [PARAMS.model_dump()]})
    )
    return _dir


@pytest.fixture
def store(tmp_path: Path) -> ParameterSetStore:
    _fstab = tmp_path / "fstab"
    _fstab.write_text("")
    return ParameterSetStore(
        root=tmp_path / "store",
        fstab=_fstab,
        mounter=FakeMounter(),
        runner=FakeRunner(),
    )


@pytest.fixture
def boot_controller(mocker: MockerFixture):
    _bc = mocker.MagicMock(spec=BootControllerBase)
    _bc.load_recorded_version.return_value = (
        "1.0.0",
        VersionDetail(release_name="base", release_id="b", image_id="base-image"),
    )
    return _bc


@pytest.fixture(autouse=True)
def running_image(mocker: MockerFixture):
    mocker.patch.object(
        _parameter_set,
        "load_image_version_detail",
        return_value=VersionDetail(image_id="base-image"),
    )


def make_updater(
    tmp_path: Path,
    *,
    blobs: Path,
    index: ImageIndex,
    store,
    image_meta,
    boot_controller,
    abort_handler: FakeAbortHandler | None = None,
) -> OTAUpdaterForParameterSet:
    from otaclient_common.downloader import DownloaderPool

    _updater = OTAUpdaterForParameterSet(
        ca_store=CAStoreMap(),
        boot_controller=boot_controller,
        abort_handler=abort_handler or FakeAbortHandler(),  # type: ignore[arg-type]
        image_identifier=THIS_ECU,
        store=store,
        image_meta_dir=image_meta,
        version="2026.10.29-1",
        raw_url_base="http://example.com/image/",
        session_wd=tmp_path / "session",
        downloader_pool=DownloaderPool(instance_num=1, hash_func=sha256),
        ecu_status_flags=MultipleECUStatusFlags(
            any_child_ecu_in_update=threading.Event(),
            any_requires_network=threading.Event(),
            all_success=threading.Event(),
        ),
        status_report_queue=Queue(),
        session_id="session",
        metrics=OTAMetricsData(),
        shm_metrics_reader=None,  # type: ignore[arg-type]
        release_name="params-2026.10.29-1",
        release_id="rid",
        image_id="parameter-image",
    )

    def _verified_index(condition):
        _updater._ota_image_helper.image_index = index
        return
        yield

    _updater._ota_image_helper.download_and_verify_image_index = _verified_index  # type: ignore[method-assign]

    def _download(batches, cond):
        """Serve every blob from `blobs`, as the download helper does: one batch at a
        time, the producer woken by another thread once the batch is down."""
        for _batch in batches:
            for _info in _batch:
                shutil.copyfile(blobs / _info.url.rsplit("/", 1)[1], _info.dst)

            def _wake():
                with cond:
                    cond.notify_all()

            threading.Timer(0.01, _wake).start()

    _updater._download = _download  # type: ignore[method-assign]
    return _updater


def phases(updater: OTAUpdaterForParameterSet) -> list[UpdatePhase]:
    _queue = updater._status_report_queue
    _phases = []
    while not _queue.empty():
        _payload = _queue.get_nowait().payload
        if isinstance(_payload, OTAUpdatePhaseChangeReport):
            _phases.append(_payload.new_update_phase)
    return _phases


class TestParameterSetUpdate:
    def test_the_set_is_laid_over_the_running_base_and_recorded(
        self, tmp_path: Path, store, image_meta, boot_controller
    ):
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=set_tree(tmp_path / "tree", "max_speed: 12\n")
        )
        _updater = make_updater(
            tmp_path,
            blobs=_blobs,
            index=_index,
            store=store,
            image_meta=image_meta,
            boot_controller=boot_controller,
        )

        assert _updater.execute() is False

        _record = store.applied_record()
        assert _record is not None
        assert _record.base_manifest_digest == str(BASE_DIGEST)
        assert _record.image_id == "parameter-image"
        assert _record.base_image_id == "base-image"
        assert _record.base_version == "1.0.0"
        assert (
            store.applied_dir
            / TREE_DNAME
            / PARAMS.relpath
            / "default"
            / "vehicle.param.yaml"
        ).read_text() == "max_speed: 12\n"
        assert PARAMS.path in store._mounter.mounted  # type: ignore[attr-defined]
        assert store._runner.ran == [PARAMS.apply]  # type: ignore[attr-defined]
        boot_controller.on_parameter_set_applied.assert_called_once_with(
            version="2026.10.29-1",
            version_detail=VersionDetail(
                release_name="params-2026.10.29-1",
                release_id="rid",
                image_id="parameter-image",
            ),
        )
        boot_controller.pre_update.assert_not_called()
        assert phases(_updater) == [
            UpdatePhase.INITIALIZING,
            UpdatePhase.PROCESSING_METADATA,
            UpdatePhase.DOWNLOADING_OTA_FILES,
            UpdatePhase.APPLYING_UPDATE,
            UpdatePhase.PROCESSING_POSTUPDATE,
        ]
        assert not (tmp_path / "session").exists()
        assert not store.staging_dir.exists()

    def test_a_second_set_keeps_the_base_records_of_the_first(
        self, tmp_path: Path, store, image_meta, boot_controller
    ):
        for _n, _content in enumerate(("a: 1\n", "a: 2\n")):
            _blobs, _index = parameter_image(
                tmp_path / f"image{_n}", tree=set_tree(tmp_path / f"tree{_n}", _content)
            )
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                image_meta=image_meta,
                boot_controller=boot_controller,
            ).execute()
            boot_controller.load_recorded_version.return_value = ("2026.10.29-1", None)

        _record = store.applied_record()
        assert _record is not None and _record.base_version == "1.0.0"

    def test_an_empty_set_records_its_release_and_clears_the_slots(
        self, tmp_path: Path, store, image_meta, boot_controller
    ):
        _blobs, _index = parameter_image(tmp_path / "image", tree=None)
        make_updater(
            tmp_path,
            blobs=_blobs,
            index=_index,
            store=store,
            image_meta=image_meta,
            boot_controller=boot_controller,
        ).execute()

        _record = store.applied_record()
        assert _record is not None and _record.slots == []
        assert _record.image_id == "parameter-image"
        assert store._mounter.mounted == {}  # type: ignore[attr-defined]
        boot_controller.on_parameter_set_applied.assert_called_once()

    def test_a_set_for_another_base_is_refused_before_anything_changes(
        self, tmp_path: Path, store, image_meta, boot_controller
    ):
        _blobs, _index = parameter_image(
            tmp_path / "image",
            tree=set_tree(tmp_path / "tree", "x\n"),
            base=Sha256Digest("0" * 64),
        )
        with pytest.raises(ota_errors.OTAImageInvalid, match="does not fit"):
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                image_meta=image_meta,
                boot_controller=boot_controller,
            ).execute()
        assert store.applied_record() is None
        boot_controller.on_parameter_set_applied.assert_not_called()
        assert "update_failed" in store.history_file.read_text()

    def test_a_base_installed_without_its_manifest_kept_takes_no_set(
        self, tmp_path: Path, store, boot_controller
    ):
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=set_tree(tmp_path / "tree", "x\n")
        )
        with pytest.raises(ota_errors.OTAImageInvalid, match="full OTA comes first"):
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                image_meta=tmp_path / "no-image-meta",
                boot_controller=boot_controller,
            ).execute()

    def test_a_failed_apply_is_a_failed_update_that_left_the_slot_as_it_was(
        self, tmp_path: Path, store, image_meta, boot_controller
    ):
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=set_tree(tmp_path / "tree", "x\n")
        )
        store._runner.fail_times = 1  # type: ignore[attr-defined]
        with pytest.raises(ota_errors.ApplyOTAUpdateFailed):
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                image_meta=image_meta,
                boot_controller=boot_controller,
            ).execute()
        assert store.applied_record() is None
        assert store._mounter.mounted == {}  # type: ignore[attr-defined]
        boot_controller.on_parameter_set_applied.assert_not_called()

    def test_a_layer_other_than_its_digest_is_an_invalid_image(
        self, tmp_path: Path, store, image_meta, boot_controller
    ):
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=set_tree(tmp_path / "tree", "x\n")
        )
        _manifest = ParameterSetManifest.parse_metafile(
            (_blobs / _index.manifests[0].digest.digest_hex).read_text()
        )
        _layer = _blobs / _manifest.layers[0].digest.digest_hex
        _layer.write_bytes(_layer.read_bytes()[:-1] + b"\0")
        with pytest.raises(ota_errors.OTAImageInvalid, match="layer is invalid"):
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                image_meta=image_meta,
                boot_controller=boot_controller,
            ).execute()
        assert store.applied_record() is None and not store.staging_dir.exists()

    def test_a_config_whose_files_are_not_the_layers_is_refused(
        self, tmp_path: Path, store, image_meta, boot_controller
    ):
        _wrong = [
            ParameterSetFileEntry(
                path=f"{PARAMS.relpath}/default/vehicle.param.yaml",
                size=1,
                digest=Sha256Digest("0" * 64),
            )
        ]
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=set_tree(tmp_path / "tree", "x\n"), files=_wrong
        )
        with pytest.raises(ota_errors.OTAImageInvalid, match="other files"):
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                image_meta=image_meta,
                boot_controller=boot_controller,
            ).execute()
        assert store.applied_record() is None

    def test_a_config_without_files_is_applied_and_holds_at_the_next_start(
        self, tmp_path: Path, store, image_meta, boot_controller
    ):
        """`files` is optional: what is checked later is what was unpacked."""
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=set_tree(tmp_path / "tree", "x\n"), files=[]
        )
        make_updater(
            tmp_path,
            blobs=_blobs,
            index=_index,
            store=store,
            image_meta=image_meta,
            boot_controller=boot_controller,
        ).execute()
        _record = store.applied_record()
        assert (
            store.check_at_startup(
                load_running_base(image_meta), slot_records=boot_controller
            )
            == _record
        )

    def test_an_index_without_this_ecus_set_is_an_invalid_image(
        self, tmp_path: Path, store, image_meta, boot_controller
    ):
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=None, ecu="perception"
        )
        with pytest.raises(ota_errors.OTAImageInvalid, match="no parameter set"):
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                image_meta=image_meta,
                boot_controller=boot_controller,
            ).execute()

    def test_a_release_that_cannot_be_recorded_puts_the_slot_back(
        self, tmp_path: Path, store, image_meta, boot_controller
    ):
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=set_tree(tmp_path / "tree", "x\n")
        )
        boot_controller.on_parameter_set_applied.side_effect = OSError("disk full")
        with pytest.raises(ota_errors.PostUpdateFailed):
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                image_meta=image_meta,
                boot_controller=boot_controller,
            ).execute()
        assert store.applied_record() is None
        assert store._mounter.mounted == {}  # type: ignore[attr-defined]
        assert not store.previous_dir.exists()

    def test_an_abort_before_the_switch_leaves_the_slot_alone(
        self, tmp_path: Path, store, image_meta, boot_controller
    ):
        """From the switch on, an abort is refused; one already in progress stops the
        update before it."""
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=set_tree(tmp_path / "tree", "x\n")
        )
        with pytest.raises(ota_errors.OTAAbortSignal):
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                image_meta=image_meta,
                boot_controller=boot_controller,
                abort_handler=FakeAbortHandler(abort_at_final_phase=True),
            ).execute()
        assert store.applied_record() is None and not store.staging_dir.exists()
        assert store._mounter.mounted == {}  # type: ignore[attr-defined]
        boot_controller.on_parameter_set_applied.assert_not_called()
