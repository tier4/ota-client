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
import os
import shutil
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
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
    ParameterLayerDescriptor,
    ParameterSetConfig,
    ParameterSetFileEntry,
    ParameterSetManifest,
)
from ota_image_libs.v1.parameter_set.slots import ParameterSlot
from ota_image_libs.v1.update_agent_package.schema import EMPTY_JSON
from pytest_mock import MockerFixture

from ota_metadata.utils.cert_store import CAStoreMap
from otaclient import errors as ota_errors
from otaclient._status_monitor import OTAUpdatePhaseChangeReport
from otaclient._types import (
    FailureType,
    MultipleECUStatusFlags,
    UpdatePhase,
    VersionDetail,
)
from otaclient.boot_control._base import BootControllerBase
from otaclient.metrics import OTAMetricsData
from otaclient.ota_core import _parameter_set
from otaclient.ota_core._parameter_set import OTAUpdaterForParameterSet
from otaclient.parameter_set import (
    TREE_DNAME,
    AppliedRecord,
    ParameterSetStore,
    RecordedSlot,
)

PARAMS = ParameterSlot(
    name="individual_params",
    path="/opt/autoware/vehicle/sample_vehicle/individual_params",
    apply="/opt/autoware/vehicle/sample_vehicle/apply.sh",
)
OVERLAY = ParameterSlot(
    name="sample_overlay",
    path="/opt/autoware/vehicle/sample_overlay",
    apply="/opt/autoware/vehicle/sample_overlay/apply.sh",
)
"""A vehicle overlay unpacked as a whole, with the apply.sh it carries."""
THIS_ECU = ImageIdentifier("autoware", OTAReleaseKey.dev)
ANNOTATIONS = {
    PLATFORM_ECU: "autoware",
    OTA_RELEASE_KEY: "dev",
    PARAMETER_SET_VERSION: "2026.10.29-1",
}


def parameter_image(
    root: Path,
    *,
    tree: Path | None,
    files: list | None | str = "the layer's",
    ecu: str = "autoware",
    slots: list[ParameterSlot] | None = None,
) -> tuple[Path, ImageIndex]:
    """The blobs of a parameter image with one set for THIS_ECU, and its index."""
    _blobs = root / "blobs"
    _blobs.mkdir(parents=True)
    if tree is None:
        _layer = EmptyDescriptor.add_contents_to_resource_dir(EMPTY_JSON, _blobs)
        _config = ParameterSetConfig(name="p", version="2026.10.29-1")
    else:
        _slots = slots or [PARAMS]
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
            files=_info.files if files == "the layer's" else files,
        )
    _manifest = ParameterSetManifest(
        config=ParameterSetConfig.Descriptor.export_metafile_to_resource_dir(
            _config, _blobs
        ),
        layers=[_layer],
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


def overlay_tree(
    root: Path, *, apply_mode: int | None = 0o755, dir_mode: int = 0o755
) -> Path:
    """OVERLAY's contents, with its apply.sh unless `apply_mode` is None."""
    _f = root / OVERLAY.relpath / "share" / "params.yaml"
    _f.parent.mkdir(parents=True)
    (root / OVERLAY.relpath).chmod(dir_mode)
    _f.write_text("a: 1\n")
    if apply_mode is not None:
        _apply = root / OVERLAY.apply[1:]
        _apply.write_text("#!/bin/sh\n")
        _apply.chmod(apply_mode)
    return root


def set_tree(root: Path, content: str) -> Path:
    _f = root / PARAMS.relpath / "default" / "vehicle.param.yaml"
    _f.parent.mkdir(parents=True)
    _f.write_text(content)
    return root


class FakeMounter:
    def __init__(self, host_root: Path) -> None:
        self.host_root = host_root
        self.mounted: dict[str, Path] = {}
        self.made: list[str] = []

    def is_mounted(self, mount_point: str) -> bool:
        return mount_point in self.mounted

    def make_mount_point(self, mount_point: str) -> bool:
        _dir = self.host_root / mount_point.lstrip("/")
        if _dir.is_dir():
            return False
        _dir.mkdir(parents=True)
        self.made.append(mount_point)
        return True

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
def host_root(tmp_path: Path) -> Path:
    """The running image: the slot's directory with its defaults, and the apply."""
    _root = tmp_path / "host_root"
    (_root / PARAMS.relpath / "default").mkdir(parents=True)
    _apply = _root / PARAMS.apply[1:]
    _apply.write_text("#!/bin/sh\n")
    _apply.chmod(0o755)
    return _root


@pytest.fixture
def store(tmp_path: Path) -> ParameterSetStore:
    _fstab = tmp_path / "fstab"
    _fstab.write_text("")
    return ParameterSetStore(
        root=tmp_path / "store",
        fstab=_fstab,
        mounter=FakeMounter(tmp_path / "host_root"),
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
    host_root,
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
        host_root=host_root,
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
        self, tmp_path: Path, store, host_root, boot_controller
    ):
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=set_tree(tmp_path / "tree", "max_speed: 12\n")
        )
        _updater = make_updater(
            tmp_path,
            blobs=_blobs,
            index=_index,
            store=store,
            host_root=host_root,
            boot_controller=boot_controller,
        )

        assert _updater.execute() is False

        _record = store.applied_record()
        assert _record is not None
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

        # until the set is in place, the slot reports the update as failed
        _calls = [_c[0] for _c in boot_controller.mock_calls]
        assert _calls.index("store_failure_info") < _calls.index(
            "on_parameter_set_applied"
        )
        _marker = boot_controller.store_failure_info.call_args.kwargs
        assert _marker["failure_reason"].startswith("E544: ")
        assert "did not finish" in _marker["failure_reason"]

        # what others read of the record, as docs/parameter_set.md says
        _raw = json.loads((store.applied_dir / "record.json").read_text())
        assert {
            _k: _raw[_k]
            for _k in ("schema_version", "image_id", "set_name", "set_version")
        } == {
            "schema_version": 1,
            "image_id": "parameter-image",
            "set_name": "p",
            "set_version": "2026.10.29-1",
        }
        assert _raw["base_image_id"] == "base-image"
        assert datetime.fromisoformat(_raw["applied_at"]).utcoffset() == timedelta(0)

    def test_a_second_set_keeps_the_base_records_of_the_first(
        self, tmp_path: Path, store, host_root, boot_controller
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
                host_root=host_root,
                boot_controller=boot_controller,
            ).execute()
            boot_controller.load_recorded_version.return_value = ("2026.10.29-1", None)

        _record = store.applied_record()
        assert _record is not None and _record.base_version == "1.0.0"

    def test_an_empty_set_records_its_release_and_clears_the_slots(
        self, tmp_path: Path, store, host_root, boot_controller
    ):
        _blobs, _index = parameter_image(tmp_path / "image", tree=None)
        make_updater(
            tmp_path,
            blobs=_blobs,
            index=_index,
            store=store,
            host_root=host_root,
            boot_controller=boot_controller,
        ).execute()

        _record = store.applied_record()
        assert _record is not None and _record.slots == []
        assert _record.image_id == "parameter-image"
        assert store._mounter.mounted == {}  # type: ignore[attr-defined]
        boot_controller.on_parameter_set_applied.assert_called_once()

    @pytest.mark.parametrize(
        "spoil, match",
        (
            pytest.param(
                lambda root: (
                    shutil.rmtree(root / PARAMS.relpath),
                    (root / PARAMS.relpath).write_text("a file\n"),
                ),
                "is not a directory",
                id="slot_path_a_file",
            ),
            pytest.param(
                lambda root: (root / PARAMS.apply[1:]).unlink(),
                "no apply",
                id="no_apply",
            ),
            pytest.param(
                lambda root: (root / PARAMS.apply[1:]).chmod(0o644),
                "not an executable",
                id="apply_not_executable",
            ),
        ),
    )
    def test_a_set_the_image_has_no_room_for_is_refused_before_anything_changes(
        self, tmp_path: Path, store, host_root, boot_controller, spoil, match
    ):
        spoil(host_root)
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=set_tree(tmp_path / "tree", "x\n")
        )
        with pytest.raises(ota_errors.ParameterSetNotApplicable, match=match) as _e:
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                host_root=host_root,
                boot_controller=boot_controller,
            ).execute()
        assert _e.value.get_failure_reason().startswith("E463: ")
        assert _e.value.failure_type == FailureType.RECOVERABLE
        assert store.applied_record() is None
        boot_controller.on_parameter_set_applied.assert_not_called()
        assert "update_failed" in store.history_file.read_text()

    def test_a_slot_something_else_is_mounted_at_is_refused(
        self, tmp_path: Path, store, host_root, boot_controller
    ):
        store._mounter.mounted[PARAMS.path] = Path("/elsewhere")  # type: ignore[attr-defined]
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=set_tree(tmp_path / "tree", "x\n")
        )
        with pytest.raises(
            ota_errors.ParameterSetNotApplicable, match="something else is mounted"
        ):
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                host_root=host_root,
                boot_controller=boot_controller,
            ).execute()
        assert store._mounter.mounted == {PARAMS.path: Path("/elsewhere")}  # type: ignore[attr-defined]

    def test_a_slot_inside_a_slot_of_the_applied_set_is_refused(
        self, tmp_path: Path, store, host_root, boot_controller
    ):
        """The applied set hides the base there: what the base has cannot be told."""
        _outer = os.path.dirname(PARAMS.path)
        store.applied_dir.mkdir(parents=True)
        (store.applied_dir / "record.json").write_text(
            AppliedRecord(
                set_manifest_digest="sha256:" + "0" * 64,
                set_name="before",
                set_version="1",
                slots=[RecordedSlot(name="outer", path=_outer, apply="reboot")],
            ).model_dump_json()
        )
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=set_tree(tmp_path / "tree", "x\n")
        )
        with pytest.raises(ota_errors.ParameterSetNotApplicable, match="inside"):
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                host_root=host_root,
                boot_controller=boot_controller,
            ).execute()
        boot_controller.store_failure_info.assert_not_called()

    @pytest.mark.parametrize(
        "path", ("/run/otaclient/x", "/ota-cache", "/proc/x", "/.ota-resources/x")
    )
    def test_a_slot_over_what_otaclient_uses_for_itself_is_refused(
        self, tmp_path: Path, store, host_root, boot_controller, path: str
    ):
        _updater = make_updater(
            tmp_path,
            blobs=tmp_path,
            index=ImageIndex(
                manifests=[],
                annotations=ImageIndex.Annotations.model_validate(
                    {BUILD_TOOL_VERSION: "t"}
                ),
            ),
            store=store,
            host_root=host_root,
            boot_controller=boot_controller,
        )
        _updater.config = ParameterSetConfig(
            name="p",
            version="1",
            slots=[ParameterSlot(name="s", path=path, apply="reboot")],
            diff_ids=[Sha256Digest("1" * 64)],
        )
        with pytest.raises(
            ota_errors.ParameterSetNotApplicable, match="reads for itself"
        ):
            _updater._check_fits_the_running_base()

    def test_an_apply_others_can_change_is_recorded_not_refused(
        self, tmp_path: Path, store, host_root, boot_controller
    ):
        (host_root / PARAMS.apply[1:]).chmod(0o775)
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=set_tree(tmp_path / "tree", "x\n")
        )
        make_updater(
            tmp_path,
            blobs=_blobs,
            index=_index,
            store=store,
            host_root=host_root,
            boot_controller=boot_controller,
        ).execute()
        _notes = [
            json.loads(_l)
            for _l in store.history_file.read_text().splitlines()
            if '"apply_not_root_only"' in _l
        ]
        assert _notes and PARAMS.apply in _notes[0]["writable"]
        assert store.applied_record() is not None

    def test_a_slot_of_the_applied_set_is_its_own_mount(
        self, tmp_path: Path, store, host_root, boot_controller
    ):
        """The set before is mounted at the slot: that is otaclient's own."""
        for _n in range(2):
            _blobs, _index = parameter_image(
                tmp_path / f"image{_n}",
                tree=set_tree(tmp_path / f"tree{_n}", f"a: {_n}\n"),
            )
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                host_root=host_root,
                boot_controller=boot_controller,
            ).execute()
        assert store._runner.ran == [PARAMS.apply, PARAMS.apply]  # type: ignore[attr-defined]

    def test_a_failed_apply_is_a_failed_update_that_left_the_slot_as_it_was(
        self, tmp_path: Path, store, host_root, boot_controller
    ):
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=set_tree(tmp_path / "tree", "x\n")
        )
        store._runner.fail_times = 1  # type: ignore[attr-defined]
        with pytest.raises(ota_errors.ParameterSetApplyFailed) as _e:
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                host_root=host_root,
                boot_controller=boot_controller,
            ).execute()
        assert _e.value.get_failure_reason().startswith("E544: ")
        assert _e.value.failure_type == FailureType.RECOVERABLE, "it can be retried"
        assert store.applied_record() is None
        assert store._mounter.mounted == {}  # type: ignore[attr-defined]
        boot_controller.on_parameter_set_applied.assert_not_called()

    def test_a_failed_apply_that_cannot_be_undone_is_unrecoverable(
        self, tmp_path: Path, store, host_root, boot_controller
    ):
        """The set before does not apply again either: what the slot runs is not
        known."""
        _blobs, _index = parameter_image(
            tmp_path / "image1", tree=set_tree(tmp_path / "tree1", "x\n")
        )
        make_updater(
            tmp_path,
            blobs=_blobs,
            index=_index,
            store=store,
            host_root=host_root,
            boot_controller=boot_controller,
        ).execute()
        boot_controller.load_recorded_version.return_value = ("2026.10.29-1", None)
        _blobs, _index = parameter_image(
            tmp_path / "image2", tree=set_tree(tmp_path / "tree2", "y\n")
        )
        store._runner.fail_times = 2  # type: ignore[attr-defined]
        with pytest.raises(ota_errors.ParameterSetRestoreFailed) as _e:
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                host_root=host_root,
                boot_controller=boot_controller,
            ).execute()
        assert _e.value.get_failure_reason().startswith("E545: ")
        assert _e.value.failure_type == FailureType.UNRECOVERABLE
        assert "roll_back_failed" in store.history_file.read_text()

    def test_a_set_that_carries_its_apply_needs_none_in_the_image(
        self, tmp_path: Path, store, host_root, boot_controller
    ):
        """The overlay's directory and its apply.sh are not in the running image: the
        mount point is made, and what runs is the set's apply.sh."""
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=overlay_tree(tmp_path / "tree"), slots=[OVERLAY]
        )
        _update = dict(
            blobs=_blobs,
            index=_index,
            store=store,
            host_root=host_root,
            boot_controller=boot_controller,
        )
        make_updater(tmp_path, **_update).execute()
        _record = store.applied_record()
        assert _record is not None and _record.slots[0].apply == OVERLAY.apply
        assert OVERLAY.path in store._mounter.mounted  # type: ignore[attr-defined]
        assert store._mounter.made == [OVERLAY.path]  # type: ignore[attr-defined]
        assert store._runner.ran == [OVERLAY.apply]  # type: ignore[attr-defined]
        _history = store.history_file.read_text()
        assert _history.count('"mount_point_made"') == 1
        assert "apply_not_root_only" not in _history

        # the same set over the applied one: its apply, inside the applied set's
        #   slot, is the set's, not the image's; nothing changed, nothing runs
        boot_controller.load_recorded_version.return_value = ("2026.10.29-1", None)
        make_updater(tmp_path, **_update).execute()
        assert store._runner.ran == [OVERLAY.apply]  # type: ignore[attr-defined]
        assert store.history_file.read_text().count('"mount_point_made"') == 1

    def test_the_way_to_the_slot_of_a_carried_apply_is_looked_at(
        self, tmp_path: Path, store, host_root, boot_controller
    ):
        """The set's apply runs by its path in the image: a directory on the way to
        its slot that others can write is recorded."""
        (host_root / "opt" / "autoware").chmod(0o775)
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=overlay_tree(tmp_path / "tree"), slots=[OVERLAY]
        )
        make_updater(
            tmp_path,
            blobs=_blobs,
            index=_index,
            store=store,
            host_root=host_root,
            boot_controller=boot_controller,
        ).execute()
        _noted = [
            json.loads(_l)
            for _l in store.history_file.read_text().splitlines()
            if '"apply_not_root_only"' in _l
        ]
        assert [_n["apply"] for _n in _noted] == [OVERLAY.apply]
        assert "/opt/autoware" in _noted[0]["writable"]

    @pytest.mark.parametrize(
        "apply_mode, dir_mode, files, match",
        (
            (None, 0o755, None, "the set has no apply"),
            (0o644, 0o755, "the layer's", "not an executable file"),
            (0o757, 0o755, "the layer's", "others can write"),
            (0o755, 0o777, "the layer's", "others can write"),
        ),
        ids=("not_carried", "not_executable", "others_write", "others_write_dir"),
    )
    def test_a_set_without_a_fit_apply_of_its_own_is_an_invalid_image(
        self,
        tmp_path: Path,
        store,
        host_root,
        boot_controller,
        apply_mode,
        dir_mode,
        files,
        match,
    ):
        _blobs, _index = parameter_image(
            tmp_path / "image",
            tree=overlay_tree(
                tmp_path / "tree", apply_mode=apply_mode, dir_mode=dir_mode
            ),
            slots=[OVERLAY],
            files=files,
        )
        with pytest.raises(ota_errors.OTAImageInvalid, match=match) as _e:
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                host_root=host_root,
                boot_controller=boot_controller,
            ).execute()
        assert _e.value.get_failure_reason().startswith("E422: ")
        assert store.applied_record() is None
        assert store._mounter.mounted == {}  # type: ignore[attr-defined]
        assert store._runner.ran == []  # type: ignore[attr-defined]

    def test_a_mount_point_that_cannot_be_made_is_refused_before_anything_changes(
        self, tmp_path: Path, store, host_root, boot_controller, mocker: MockerFixture
    ):
        _statvfs = os.statvfs

        def _read_only(path):
            _st = _statvfs(path)
            return mocker.MagicMock(f_flag=_st.f_flag | os.ST_RDONLY)

        mocker.patch.object(_parameter_set.os, "statvfs", side_effect=_read_only)
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=overlay_tree(tmp_path / "tree"), slots=[OVERLAY]
        )
        with pytest.raises(
            ota_errors.ParameterSetNotApplicable, match="cannot be made"
        ):
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                host_root=host_root,
                boot_controller=boot_controller,
            ).execute()
        assert store._mounter.made == []  # type: ignore[attr-defined]

    def test_a_failed_first_set_is_recoverable(
        self, tmp_path: Path, store, host_root, boot_controller
    ):
        """Nothing before to put back, so nothing runs again: the slot is the base's
        once more, the update can be retried."""
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=set_tree(tmp_path / "tree", "x\n")
        )
        store._runner.fail_times = 2  # type: ignore[attr-defined]
        with pytest.raises(ota_errors.ParameterSetApplyFailed) as _e:
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                host_root=host_root,
                boot_controller=boot_controller,
            ).execute()
        assert _e.value.get_failure_reason().startswith("E544: ")
        assert _e.value.failure_type == FailureType.RECOVERABLE
        assert store._runner.ran == [PARAMS.apply]  # type: ignore[attr-defined]

    def test_a_failure_before_the_switch_changes_nothing(
        self, tmp_path: Path, store, host_root, boot_controller, mocker: MockerFixture
    ):
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=set_tree(tmp_path / "tree", "x\n")
        )
        mocker.patch.object(store, "stage", side_effect=OSError("no space left"))
        with pytest.raises(ota_errors.ParameterSetApplyFailed, match="nothing changed"):
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                host_root=host_root,
                boot_controller=boot_controller,
            ).execute()
        assert store.applied_record() is None and not store.staging_dir.exists()
        boot_controller.restore_recorded_version.assert_not_called()

    def test_a_layer_other_than_its_digest_is_an_invalid_image(
        self, tmp_path: Path, store, host_root, boot_controller
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
                host_root=host_root,
                boot_controller=boot_controller,
            ).execute()
        assert store.applied_record() is None and not store.staging_dir.exists()

    def test_a_config_whose_files_are_not_the_layers_is_refused(
        self, tmp_path: Path, store, host_root, boot_controller
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
                host_root=host_root,
                boot_controller=boot_controller,
            ).execute()
        assert store.applied_record() is None

    def test_a_config_without_files_is_applied_and_holds_at_the_next_start(
        self, tmp_path: Path, store, host_root, boot_controller
    ):
        """`files` is optional: what is checked later is what was unpacked."""
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=set_tree(tmp_path / "tree", "x\n"), files=None
        )
        make_updater(
            tmp_path,
            blobs=_blobs,
            index=_index,
            store=store,
            host_root=host_root,
            boot_controller=boot_controller,
        ).execute()
        _record = store.applied_record()
        assert _record is not None
        assert store.check_tree(_record) is None, "the files unpacked are listed"
        assert (
            store.check_at_startup("base-image", slot_records=boot_controller)
            == _record
        )

    def test_an_index_without_this_ecus_set_is_an_invalid_image(
        self, tmp_path: Path, store, host_root, boot_controller
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
                host_root=host_root,
                boot_controller=boot_controller,
            ).execute()

    def test_a_release_that_cannot_be_recorded_puts_the_slot_back(
        self, tmp_path: Path, store, host_root, boot_controller
    ):
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=set_tree(tmp_path / "tree", "x\n")
        )
        boot_controller.on_parameter_set_applied.side_effect = OSError("disk full")
        with pytest.raises(ota_errors.ParameterSetApplyFailed, match="recorded"):
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                host_root=host_root,
                boot_controller=boot_controller,
            ).execute()
        assert store.applied_record() is None
        assert store._mounter.mounted == {}  # type: ignore[attr-defined]
        assert not store.previous_dir.exists()
        # the slot reports the release it had before the update again
        boot_controller.restore_recorded_version.assert_called_once_with(
            version="1.0.0",
            version_detail=VersionDetail(
                release_name="base", release_id="b", image_id="base-image"
            ),
        )

    def test_records_that_cannot_be_put_back_are_unrecoverable(
        self, tmp_path: Path, store, host_root, boot_controller
    ):
        _blobs, _index = parameter_image(
            tmp_path / "image", tree=set_tree(tmp_path / "tree", "x\n")
        )
        boot_controller.on_parameter_set_applied.side_effect = OSError("disk full")
        boot_controller.restore_recorded_version.side_effect = OSError("disk full")
        with pytest.raises(ota_errors.ParameterSetRestoreFailed):
            make_updater(
                tmp_path,
                blobs=_blobs,
                index=_index,
                store=store,
                host_root=host_root,
                boot_controller=boot_controller,
            ).execute()
        assert store.applied_record() is None, "the slots are back all the same"
        assert "records_not_restored" in store.history_file.read_text()

    def test_an_abort_before_the_switch_leaves_the_slot_alone(
        self, tmp_path: Path, store, host_root, boot_controller
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
                host_root=host_root,
                boot_controller=boot_controller,
                abort_handler=FakeAbortHandler(abort_at_final_phase=True),
            ).execute()
        assert store.applied_record() is None and not store.staging_dir.exists()
        assert store._mounter.mounted == {}  # type: ignore[attr-defined]
        boot_controller.on_parameter_set_applied.assert_not_called()
