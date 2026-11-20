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
"""The parameter set store: the switch, the mounts at boot, the check at startup and
the history."""

from __future__ import annotations

import json
import logging
import os
import runpy
import stat
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest
from ota_image_libs.common.oci_spec import Sha256Digest
from ota_image_libs.v1.parameter_set.schema import ParameterSetFileEntry
from ota_image_libs.v1.parameter_set.slots import ParameterSlot
from pytest_mock import MockerFixture

from otaclient import parameter_set
from otaclient._types import VersionDetail
from otaclient.boot_control import _image_version_file
from otaclient.parameter_set import (
    HISTORY_MAX_SIZE,
    TREE_DNAME,
    AppliedRecord,
    CommandFailed,
    HostParameterSlotMounter,
    ParameterSetFailed,
    ParameterSetStore,
    RecordedSlot,
    on_the_active_root_filesystem,
    run_apply,
    run_command,
    why_not_private,
)
from otaclient_common import _io

PARAMS = ParameterSlot(
    name="individual_params",
    path="/opt/autoware/vehicle/sample_vehicle/individual_params",
    apply="/opt/autoware/bin/apply-parameter-set",
)
LAUNCH = ParameterSlot(
    name="launch",
    path="/opt/autoware/vehicle/sample_vehicle/launch",
    apply="/opt/autoware/bin/apply-launch",
)
REBOOT = ParameterSlot(name="kernel", path="/opt/kernel-params", apply="reboot")
BASE_ID = "base-image"
BASE_DETAIL = VersionDetail(release_name="base", release_id="b", image_id="base-image")


class FakeMounter:
    """Mounts as the kernel keeps them: a bind is of the directory it was made from
    (its inode), whatever that directory is renamed to later."""

    def __init__(self) -> None:
        self.mounted: dict[str, tuple[Path, int]] = {}
        self.fail_mount = False
        self.fail_umount: bool | set[str] = False
        self.missing: set[str] = set()
        """The slot directories the running image does not have."""
        self.made: list[str] = []

    def is_mounted(self, mount_point: str) -> bool:
        return mount_point in self.mounted

    def make_mount_point(self, mount_point: str) -> bool:
        if mount_point not in self.missing:
            return False
        self.missing.discard(mount_point)
        self.made.append(mount_point)
        return True

    def bind_ro(self, source: Path, mount_point: str) -> None:
        if self.fail_mount:
            raise OSError("mount failed")
        assert source.is_dir(), f"{source} is not there"
        assert mount_point not in self.mounted, "bound twice"
        self.mounted[mount_point] = (source, source.stat().st_ino)

    def umount(self, mount_point: str) -> None:
        if self.fail_umount is True or (
            isinstance(self.fail_umount, set) and mount_point in self.fail_umount
        ):
            raise OSError("umount failed")
        del self.mounted[mount_point]

    def shows(self, mount_point: str, directory: Path) -> bool:
        """Whether the bind at `mount_point` is of `directory` as it is now."""
        return self.mounted[mount_point][1] == directory.stat().st_ino


class FakeRunner:
    def __init__(self) -> None:
        self.ran: list[str] = []
        self.fail_times = 0

    def __call__(self, command: str) -> None:
        self.ran.append(command)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError(f"{command} failed")


class FakeSlotRecords:
    """The slot's version records, as the boot controller keeps them."""

    def __init__(self, version: str, detail: VersionDetail | None) -> None:
        self.version, self.detail = version, detail

    def load_recorded_version(self):
        return self.version, self.detail

    def restore_recorded_version(self, *, version, version_detail) -> None:
        self.version, self.detail = version, version_detail


@pytest.fixture(autouse=True)
def tmp_path_is_a_mount_point(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The way to a store is checked from the mount point it is on down, and the real
    mount point of tmp_path has /tmp on the way, which everyone can write: here
    tmp_path, private to us, stands in for it. What a test makes there is made under
    the umask otaclient runs under, systemd's."""
    _real = _io._mount_point_of
    monkeypatch.setattr(
        _io,
        "_mount_point_of",
        lambda _p: tmp_path if tmp_path in (_p, *_p.parents) else _real(_p),
    )
    _old_umask = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(_old_umask)


@pytest.fixture(autouse=True)
def on_the_slots_own_root(mocker: MockerFixture):
    """Whether the store is on the running slot's root filesystem: yes, unless a test
    says the slots share it."""
    return mocker.patch.object(
        parameter_set, "on_the_active_root_filesystem", return_value=True
    )


@pytest.fixture
def mounter() -> FakeMounter:
    return FakeMounter()


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def store(
    tmp_path: Path, mounter: FakeMounter, runner: FakeRunner
) -> ParameterSetStore:
    return ParameterSetStore(
        root=tmp_path / "opt/data/ota/parameter-set",
        mounter=mounter,
        runner=runner,
    )


def stage(
    store: ParameterSetStore,
    *slots: ParameterSlot,
    content: str = "v1",
    contents: dict[str, str] | None = None,
    base_image_id: str = BASE_ID,
) -> AppliedRecord:
    """A set in staging, as the updater leaves it: the tree, its files, the record."""
    _staging = store.prepare_staging()
    _files = []
    for _slot in slots:
        _text = (contents or {}).get(_slot.name, content)
        _f = _staging / TREE_DNAME / _slot.relpath / "default" / "vehicle.param.yaml"
        _f.parent.mkdir(parents=True)
        _f.write_text(f"{_slot.name}: {_text}\n")
        _files.append(
            ParameterSetFileEntry(
                path=f"{_slot.relpath}/default/vehicle.param.yaml",
                size=_f.stat().st_size,
                digest=Sha256Digest(sha256(_f.read_bytes()).hexdigest()),
            )
        )
    _record = AppliedRecord(
        set_manifest_digest=f"sha256:{sha256(content.encode()).hexdigest()}",
        set_name="p",
        set_version=content,
        slots=list(slots),
        base_image_id=base_image_id,
        firmware_version=content,
        release_name=f"release-{content}",
        release_id="rid",
        image_id=f"parameter-image-{content}",
        base_version="1.0.0",
        base_version_detail=BASE_DETAIL,
    )
    store.stage(_record, _files)
    return _record


def apply_set(store: ParameterSetStore, *slots, **kw) -> AppliedRecord:
    _record = stage(store, *slots, **kw)
    store.switch()
    store.commit()
    return _record


def reboot(mounter: FakeMounter) -> None:
    """A reboot: no mount survives it."""
    mounter.mounted.clear()


def history(store: ParameterSetStore) -> list[dict]:
    return [json.loads(_l) for _l in store.history_file.read_text().splitlines()]


def events(store: ParameterSetStore) -> list[str]:
    return [_e["event"] for _e in history(store)] if store.history_file.exists() else []


class TestSwitch:
    def test_a_set_is_bound_over_its_slots_now_and_at_boot(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        _record = stage(store, PARAMS, LAUNCH)
        assert store.switch() is False
        store.commit()

        assert store.applied_record() == _record
        assert not store.staging_dir.exists() and not store.previous_dir.exists()
        assert set(mounter.mounted) == {PARAMS.path, LAUNCH.path}
        assert mounter.shows(
            PARAMS.path, store.applied_dir / TREE_DNAME / PARAMS.relpath
        )
        assert runner.ran == [PARAMS.apply, LAUNCH.apply]
        _event = history(store)[-1]
        assert _event["event"] == "applied"
        assert _event["image_id"] == "parameter-image-v1"
        assert _event["base_image_id"] == "base-image"

        # the boot after: the boot-time unit lays applied/ again, and runs nothing
        reboot(mounter)
        runner.ran.clear()
        assert store.mount_at_boot(BASE_ID) is True
        for _slot in (PARAMS, LAUNCH):
            assert mounter.shows(_slot.path, store.tree_of(_slot))
        assert runner.ran == []

    def test_only_the_slots_whose_files_change_are_applied(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        apply_set(store, PARAMS, LAUNCH)
        runner.ran.clear()
        _new = apply_set(
            store,
            PARAMS,
            LAUNCH,
            content="v2",
            contents={LAUNCH.name: "v1"},
        )

        assert store.applied_record() == _new
        assert runner.ran == [PARAMS.apply], "launch holds the same files"
        assert set(mounter.mounted) == {PARAMS.path, LAUNCH.path}
        # bound again, from the new applied/: a bind made before the switch would
        #   still be of the tree now in previous/, which the commit deletes
        for _slot in (PARAMS, LAUNCH):
            assert mounter.shows(_slot.path, store.tree_of(_slot))

    def test_the_same_set_again_applies_nothing(
        self, store: ParameterSetStore, runner: FakeRunner
    ):
        apply_set(store, REBOOT, PARAMS)
        runner.ran.clear()
        _record = stage(store, REBOOT, PARAMS)
        assert store.switch() is False, "no reboot"
        assert runner.ran == []

    def test_a_slot_the_base_has_no_directory_for_gets_a_mount_point(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        mounter.missing.add(PARAMS.path)
        apply_set(store, PARAMS)
        assert mounter.made == [PARAMS.path]
        assert PARAMS.path in mounter.mounted
        assert "mount_point_made" in events(store)
        _made = [_e for _e in history(store) if _e["event"] == "mount_point_made"]
        assert [(_e["slot"], _e["path"]) for _e in _made] == [
            (PARAMS.name, PARAMS.path)
        ]
        # nothing made: nothing recorded
        apply_set(store, PARAMS, content="v2")
        assert events(store).count("mount_point_made") == 1

    def test_a_new_set_without_a_slot_gives_it_back_the_base_defaults(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        apply_set(store, PARAMS, LAUNCH)
        runner.ran.clear()
        apply_set(store, PARAMS)

        assert set(mounter.mounted) == {PARAMS.path}
        assert runner.ran == [], "a slot taken away runs nothing; PARAMS is the same"

    def test_an_empty_set_gives_every_slot_the_base_defaults(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        apply_set(store, PARAMS)
        _empty = apply_set(store, content="v2")

        assert mounter.mounted == {}
        assert runner.ran == [PARAMS.apply], "nothing runs for the slot taken away"
        assert store.applied_record() == _empty, "the release is still recorded"

    def test_a_failed_apply_puts_the_old_set_back(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        _old = apply_set(store, PARAMS)
        runner.fail_times = 1

        stage(store, PARAMS, content="v2")
        with pytest.raises(ParameterSetFailed) as _e:
            store.switch()

        assert _e.value.restored
        assert store.applied_record() == _old
        assert not store.previous_dir.exists()
        _f = (
            store.applied_dir
            / TREE_DNAME
            / PARAMS.relpath
            / "default"
            / "vehicle.param.yaml"
        )
        assert _f.read_text() == "individual_params: v1\n"
        assert mounter.shows(PARAMS.path, store.tree_of(PARAMS))
        assert runner.ran[-1] == PARAMS.apply, "the apply runs again for the old set"
        assert events(store)[-1] == "rolled_back"

    def test_a_first_set_that_fails_leaves_the_base(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        mounter.fail_mount = True
        stage(store, PARAMS)
        with pytest.raises(ParameterSetFailed) as _e:
            store.switch()
        assert _e.value.restored
        assert store.applied_record() is None
        assert mounter.mounted == {}

    def test_an_unmount_that_fails_leaves_the_applied_set_where_it_was(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        """Nothing was moved yet: the old set stays, mounted."""
        _old = apply_set(store, PARAMS)
        mounter.fail_umount = True
        stage(store, PARAMS, content="v2")
        with pytest.raises(ParameterSetFailed) as _e:
            store.switch()

        assert _e.value.restored
        assert store.applied_record() == _old
        assert PARAMS.path in mounter.mounted

    def test_a_first_set_whose_apply_fails_runs_nothing_after(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        """Undone with nothing before to put back: the slot goes back to the base's
        and nothing runs for it."""
        stage(store, PARAMS)
        runner.fail_times = 1
        with pytest.raises(ParameterSetFailed) as _e:
            store.switch()
        assert _e.value.restored
        assert runner.ran == [PARAMS.apply]
        assert mounter.mounted == {}
        assert store.applied_record() is None

    def test_a_failed_first_set_is_set_aside_even_if_it_stays_mounted(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        """Undone after its apply failed, its mount not taken away: it is not the
        applied set any more, and nothing mounts it at boot."""
        stage(store, PARAMS)
        runner.fail_times = 1
        mounter.fail_umount = True
        with pytest.raises(ParameterSetFailed) as _e:
            store.switch()
        assert not _e.value.restored
        assert PARAMS.path in mounter.mounted
        assert store.applied_record() is None
        assert runner.ran == [PARAMS.apply], "nothing runs for the slot taken away"
        reboot(mounter)
        assert store.mount_at_boot(BASE_ID) is True
        assert mounter.mounted == {}, "nothing mounts it at boot"

    def test_a_set_that_cannot_be_put_back_says_so(
        self, store: ParameterSetStore, runner: FakeRunner
    ):
        """The new set's apply fails, then the old set's: the slot runs neither as
        it should."""
        apply_set(store, PARAMS)
        runner.fail_times = 2
        stage(store, PARAMS, content="v2")
        with pytest.raises(ParameterSetFailed) as _e:
            store.switch()
        assert not _e.value.restored
        assert events(store)[-1] == "roll_back_failed"

    def test_the_old_set_comes_back_when_the_release_cannot_be_recorded(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        """What the updater does when the slot's records fail after the switch."""
        _old = apply_set(store, PARAMS)
        stage(store, PARAMS, content="v2")
        store.switch()
        assert store.previous_dir.exists(), "kept until the release is recorded"

        assert store.roll_back(cause=OSError("disk full"))

        assert store.applied_record() == _old
        assert runner.ran[-1] == PARAMS.apply
        assert "applied" not in events(store)[1:]

    def test_a_slot_whose_apply_is_reboot_asks_for_one(self, store: ParameterSetStore):
        stage(store, REBOOT)
        assert store.switch() is True

    def test_the_store_and_its_parents_are_made_with_their_modes(
        self, store: ParameterSetStore
    ):
        """/opt/data and /opt/data/ota are made 0755 when missing, the store 0700,
        whatever the umask; what is there keeps its mode."""
        _opt = store.root.parents[2]
        _opt.mkdir(parents=True)
        _opt.chmod(0o750)
        _umask = os.umask(0o077)
        try:
            stage(store, PARAMS)
        finally:
            os.umask(_umask)
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        for _made in (store.root.parent, store.root.parent.parent):
            assert stat.S_IMODE(_made.stat().st_mode) == 0o755, _made
        assert stat.S_IMODE(_opt.stat().st_mode) == 0o750

    def test_the_set_put_back_runs_its_own_apply(
        self, store: ParameterSetStore, runner: FakeRunner
    ):
        """Two sets with their own apply for the same slot: the new one fails, the
        one put back is applied with its apply, not the failed set's."""
        _a = ParameterSlot(
            name=PARAMS.name, path=PARAMS.path, apply="/usr/bin/reload-a"
        )
        _b = ParameterSlot(
            name=PARAMS.name, path=PARAMS.path, apply="/usr/bin/reload-b"
        )
        _old = apply_set(store, _a)
        runner.ran.clear()
        runner.fail_times = 1
        stage(store, _b, content="v2")
        with pytest.raises(ParameterSetFailed) as _e:
            store.switch()
        assert _e.value.restored
        assert runner.ran == ["/usr/bin/reload-b", "/usr/bin/reload-a"]
        assert store.applied_record() == _old

    def test_a_slot_that_moves_is_taken_away_and_filled(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        """The same name at another path: the old path goes back to the base with
        nothing run, the new one is filled with the new apply."""
        _old = ParameterSlot(
            name="cfg", path="/opt/app/cfg", apply="/usr/bin/reload-old"
        )
        _new = ParameterSlot(
            name="cfg", path="/opt/app2/cfg", apply="/usr/bin/reload-new"
        )
        apply_set(store, _old)
        runner.ran.clear()
        apply_set(store, _new, content="v2")
        assert set(mounter.mounted) == {_new.path}
        assert runner.ran == ["/usr/bin/reload-new"]

    def test_another_apply_for_the_same_files_is_a_change(
        self, store: ParameterSetStore, runner: FakeRunner
    ):
        apply_set(store, PARAMS)
        runner.ran.clear()
        _moved = ParameterSlot(name=PARAMS.name, path=PARAMS.path, apply="/usr/bin/x")
        apply_set(store, _moved)
        assert runner.ran == ["/usr/bin/x"]

    def test_a_partly_failed_unmount_mounts_back_what_came_off(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        """The first slot comes off, the second is busy: nothing was moved yet, and
        the slot that came off is bound again."""
        _old = apply_set(store, PARAMS, LAUNCH)
        mounter.fail_umount = {LAUNCH.path}
        stage(store, PARAMS, LAUNCH, content="v2")
        with pytest.raises(ParameterSetFailed) as _e:
            store.switch()
        assert _e.value.restored
        assert store.applied_record() == _old
        for _slot in (PARAMS, LAUNCH):
            assert mounter.shows(_slot.path, store.tree_of(_slot))
        assert events(store)[-1] == "rolled_back"

    def test_the_commit_sets_the_set_before_aside_first(
        self, store: ParameterSetStore, mocker: MockerFixture
    ):
        """One rename marks the set before gone: a crash while it is deleted leaves
        discard/, never a previous/ taken for an unfinished switch."""
        apply_set(store, PARAMS)
        stage(store, PARAMS, content="v2")
        store.switch()
        _deleted: list[str] = []
        _rmtree = parameter_set.shutil.rmtree

        def _record_rmtree(path, *args, **kwargs):
            _deleted.append(Path(path).name)
            assert not store.previous_dir.exists() or Path(path).name != "previous"
            return _rmtree(path, *args, **kwargs)

        mocker.patch.object(parameter_set.shutil, "rmtree", side_effect=_record_rmtree)
        store.commit()
        assert _deleted[0] == "discard"
        assert not store.previous_dir.exists() and not store.discard_dir.exists()


class TestAtStartup:
    def records_of(self, record: AppliedRecord) -> FakeSlotRecords:
        """The slot records the set's release, as the updater left it."""
        return FakeSlotRecords(
            record.firmware_version,
            VersionDetail(
                release_name=record.release_name,
                release_id=record.release_id,
                image_id=record.image_id,
            ),
        )

    @pytest.fixture(autouse=True)
    def boot(self, mocker: MockerFixture):
        _ids = {"now": "boot-1"}
        mocker.patch.object(parameter_set, "_boot_id", side_effect=lambda: _ids["now"])

        def _set(boot_id: str) -> None:
            _ids["now"] = boot_id

        return _set

    def test_a_set_that_holds_stays_and_is_recorded_once_per_boot(
        self, store: ParameterSetStore, boot
    ):
        _record = apply_set(store, PARAMS)
        _records = self.records_of(_record)
        assert (
            store.check_at_startup(BASE_ID, base_committed=True, slot_records=_records)
            == _record
        )
        assert (
            store.check_at_startup(BASE_ID, base_committed=True, slot_records=_records)
            == _record
        )
        assert events(store).count("running") == 1, "a restart adds no line"
        boot("boot-2")
        store.check_at_startup(BASE_ID, base_committed=True, slot_records=_records)
        assert events(store).count("running") == 2

    def test_a_changed_file_is_kept_as_it_is_and_recorded(
        self,
        store: ParameterSetStore,
        mounter: FakeMounter,
        runner: FakeRunner,
        boot,
    ):
        """What the ECU runs changes by an update only: the set stays, mounted and
        reported, with the finding in the history."""
        _record = apply_set(store, PARAMS)
        runner.ran.clear()
        _f = (
            store.applied_dir
            / TREE_DNAME
            / PARAMS.relpath
            / "default"
            / "vehicle.param.yaml"
        )
        _f.write_text("tampered\n")
        _records = self.records_of(_record)

        assert (
            store.check_at_startup(BASE_ID, base_committed=True, slot_records=_records)
            == _record
        )
        assert _records.version == _record.firmware_version
        assert _records.detail and _records.detail.image_id == _record.image_id
        assert PARAMS.path in mounter.mounted
        assert _f.read_text() == "tampered\n", "not put back either"
        assert runner.ran == []
        _events = history(store)
        assert [_e["event"] for _e in _events[-2:]] == ["running", "tree_mismatch"]
        assert "differs" in _events[-1]["reason"]

        store.check_at_startup(BASE_ID, base_committed=True, slot_records=_records)
        assert events(store).count("tree_mismatch") == 1, "a restart adds no line"
        boot("boot-2")
        store.check_at_startup(BASE_ID, base_committed=True, slot_records=_records)
        assert events(store)[-2:] == ["running", "tree_mismatch"]
        assert events(store).count("tree_mismatch") == 2

    def test_a_changed_set_that_did_not_mount_at_boot_is_mounted_as_it_is(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        _record = apply_set(store, PARAMS)
        (store.applied_dir / TREE_DNAME / PARAMS.relpath / "extra").write_text("x")
        mounter.mounted.clear()
        runner.ran.clear()
        assert (
            store.check_at_startup(
                BASE_ID, base_committed=True, slot_records=self.records_of(_record)
            )
            == _record
        )
        assert PARAMS.path in mounter.mounted
        assert runner.ran == [PARAMS.apply]

    def test_the_next_update_of_a_changed_set_puts_its_files_back_and_applies(
        self, store: ParameterSetStore, runner: FakeRunner
    ):
        """The same set again: by the file lists nothing changes, but the files in
        place are not those, so every slot is put in place and applied."""
        _record = apply_set(store, PARAMS)
        _f = (
            store.applied_dir
            / TREE_DNAME
            / PARAMS.relpath
            / "default"
            / "vehicle.param.yaml"
        )
        _f.write_text("tampered\n")
        runner.ran.clear()

        apply_set(store, PARAMS)

        assert runner.ran == [PARAMS.apply]
        assert _f.read_text() == "individual_params: v1\n"
        assert store.check_tree(_record) is None

    def test_files_not_the_sets_run_only_the_new_sets_slots(
        self, store: ParameterSetStore, runner: FakeRunner
    ):
        """Every slot of the new set is applied, none of the one it takes away."""
        apply_set(store, PARAMS, LAUNCH)
        (
            store.applied_dir
            / TREE_DNAME
            / PARAMS.relpath
            / "default"
            / "vehicle.param.yaml"
        ).write_text("tampered\n")
        runner.ran.clear()
        apply_set(store, PARAMS)
        assert runner.ran == [PARAMS.apply]

    def test_the_same_set_again_is_not_applied_when_its_files_hold(
        self, store: ParameterSetStore, runner: FakeRunner
    ):
        apply_set(store, PARAMS)
        runner.ran.clear()
        apply_set(store, PARAMS)
        assert runner.ran == []

    def test_a_slot_that_records_another_release_keeps_it_on_a_drop(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        apply_set(store, PARAMS)
        del mounter.mounted[PARAMS.path]
        mounter.fail_mount = True
        _records = FakeSlotRecords("9.9.9", VersionDetail(image_id="another"))
        assert (
            store.check_at_startup(BASE_ID, base_committed=True, slot_records=_records)
            is None
        )
        assert _records.version == "9.9.9"

    @pytest.mark.parametrize(
        "running, applied_on",
        (
            pytest.param("another-image", BASE_ID, id="another_image"),
            pytest.param("", BASE_ID, id="the_version_file_is_gone"),
            pytest.param(BASE_ID, "", id="applied_on_an_image_without_one"),
        ),
    )
    def test_a_set_of_another_image_is_dropped_without_touching_the_records(
        self,
        store: ParameterSetStore,
        mounter: FakeMounter,
        runner: FakeRunner,
        running: str,
        applied_on: str,
    ):
        """A set copied in with the slot: the slot's records are the new image's. It
        was never laid on this base: what is mounted at its slots' paths is this
        base's own, and stays."""
        _record = apply_set(store, PARAMS, base_image_id=applied_on)
        reboot(mounter)
        mounter.mounted[PARAMS.path] = (Path("/the/base/own"), 0)
        runner.ran.clear()
        _records = self.records_of(_record)
        assert (
            store.check_at_startup(running, base_committed=True, slot_records=_records)
            is None
        )
        assert _records.version == _record.firmware_version
        assert store.applied_record() is None
        assert mounter.mounted == {PARAMS.path: (Path("/the/base/own"), 0)}
        assert runner.ran == []
        assert "was applied on image" in history(store)[-1]["reason"]

    def test_a_set_applied_on_an_image_without_a_version_file_stays(
        self, store: ParameterSetStore
    ):
        _record = apply_set(store, PARAMS, base_image_id="")
        assert (
            store.check_at_startup(
                "", base_committed=True, slot_records=self.records_of(_record)
            )
            == _record
        )
        assert "tree_mismatch" not in events(store)

    def test_an_unreadable_record_drops_the_set(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        """What it fills cannot be told: the boot mounts none of it, and otaclient's
        start drops it."""
        apply_set(store, PARAMS, LAUNCH)
        (store.applied_dir / "record.json").write_text("")
        reboot(mounter)
        runner.ran.clear()
        assert store.mount_at_boot(BASE_ID) is False
        assert mounter.mounted == {}
        _records = FakeSlotRecords("x", None)
        assert (
            store.check_at_startup(BASE_ID, base_committed=True, slot_records=_records)
            is None
        )
        assert not store.applied_dir.exists()
        assert mounter.mounted == {} and runner.ran == []
        assert "unreadable" in history(store)[-1]["reason"]

    def test_an_unfinished_switch_is_rolled_back(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        """Power lost after the switch, before the release was recorded."""
        _old = apply_set(store, PARAMS)
        stage(store, PARAMS, content="v2")
        store.switch()

        assert (
            store.check_at_startup(
                BASE_ID, base_committed=True, slot_records=self.records_of(_old)
            )
            == _old
        )
        assert not store.previous_dir.exists()
        assert events(store)[-3:] == ["rolled_back", "recovered", "running"]

    def test_a_drop_runs_no_apply(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        """The slot goes back to the base's when what reads it restarts."""
        _record = apply_set(store, PARAMS)
        del mounter.mounted[PARAMS.path]
        mounter.fail_mount = True
        runner.ran.clear()
        _records = self.records_of(_record)
        store.check_at_startup(BASE_ID, base_committed=True, slot_records=_records)
        assert events(store)[-1] == "dropped"
        assert runner.ran == []
        assert store.applied_record() is None
        assert _records.version == "1.0.0", "the base's records come back first"

    def test_a_slot_that_did_not_mount_at_boot_is_mounted_and_applied(
        self,
        store: ParameterSetStore,
        mounter: FakeMounter,
        runner: FakeRunner,
        caplog: pytest.LogCaptureFixture,
    ):
        """The boot-time unit is not installed, or did not mount it: otaclient's
        start does, and says so."""
        _record = apply_set(store, PARAMS, REBOOT)
        reboot(mounter)
        runner.ran.clear()

        with caplog.at_level(logging.WARNING, logger=parameter_set.__name__):
            assert (
                store.check_at_startup(
                    BASE_ID, base_committed=True, slot_records=self.records_of(_record)
                )
                == _record
            )
        assert PARAMS.path in mounter.mounted
        assert runner.ran == [PARAMS.apply]
        assert events(store)[-3:] == ["remounted", "reboot_required", "running"]
        assert "the boot-time unit" in caplog.text
        assert "did not mount them" in caplog.text

    def test_a_slot_that_cannot_be_mounted_drops_the_set(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        _record = apply_set(store, PARAMS, LAUNCH)
        del mounter.mounted[LAUNCH.path]
        mounter.fail_mount = True
        _records = self.records_of(_record)
        assert (
            store.check_at_startup(BASE_ID, base_committed=True, slot_records=_records)
            is None
        )
        assert mounter.mounted == {}, "the slot that was mounted is unmounted too"
        assert store.applied_record() is None
        assert _records.version == "1.0.0", "the slot reports the base again"
        assert events(store)[-1] == "dropped"

    def test_nothing_applied_is_nothing_to_check(self, store: ParameterSetStore):
        assert (
            store.check_at_startup(
                BASE_ID, base_committed=True, slot_records=FakeSlotRecords("", None)
            )
            is None
        )
        assert not store.history_file.exists()

    def test_a_switch_recorded_but_not_committed_is_completed(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        """Power lost after the slot recorded the new release, before previous/ was
        deleted: the new set stays, it is not rolled back under its release."""
        apply_set(store, PARAMS)
        _new = stage(store, PARAMS, content="v2")
        store.switch()
        runner.ran.clear()
        assert store.previous_dir.exists()

        _records = self.records_of(_new)
        assert (
            store.check_at_startup(BASE_ID, base_committed=True, slot_records=_records)
            == _new
        )
        assert not store.previous_dir.exists() and not store.discard_dir.exists()
        assert mounter.shows(PARAMS.path, store.tree_of(PARAMS))
        assert runner.ran == [], "the switch ran the apply"
        assert _records.version == _new.firmware_version
        assert "rolled_back" not in events(store)[-4:]
        assert [_e for _e in history(store) if _e["event"] == "recovered"][-1][
            "completed"
        ]

    def test_a_commit_cut_short_in_its_deletion_is_completed(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        """previous/ half deleted, its record already gone: the applied set is the
        one the slot records, so it stays."""
        apply_set(store, PARAMS)
        _new = stage(store, PARAMS, content="v2")
        store.switch()
        (store.previous_dir / "record.json").unlink()

        assert (
            store.check_at_startup(
                BASE_ID, base_committed=True, slot_records=self.records_of(_new)
            )
            == _new
        )
        assert not store.previous_dir.exists()
        assert mounter.shows(PARAMS.path, store.tree_of(PARAMS))

    def test_a_left_over_discard_is_deleted(self, store: ParameterSetStore):
        _record = apply_set(store, PARAMS)
        (store.discard_dir / "tree").mkdir(parents=True)
        assert (
            store.check_at_startup(
                BASE_ID, base_committed=True, slot_records=self.records_of(_record)
            )
            == _record
        )
        assert not store.discard_dir.exists()

    def test_power_lost_between_the_two_renames(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        """Only previous/ is there: the set before goes back, applied."""
        _old = apply_set(store, PARAMS)
        os.replace(store.applied_dir, store.previous_dir)
        mounter.mounted.clear()
        runner.ran.clear()

        assert (
            store.check_at_startup(
                BASE_ID, base_committed=True, slot_records=self.records_of(_old)
            )
            == _old
        )
        assert mounter.shows(PARAMS.path, store.tree_of(PARAMS))
        assert runner.ran == [PARAMS.apply]

    def test_a_drop_whose_mount_cannot_go_leaves_nothing_to_mount_at_boot(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        """A slot cannot be mounted: the set is dropped, and the mount of the slot
        that was cannot be taken away. The set left applied/ first: the next boot
        lays nothing."""
        _record = apply_set(store, PARAMS, LAUNCH)
        del mounter.mounted[LAUNCH.path]
        mounter.fail_mount = True
        mounter.fail_umount = True
        store.check_at_startup(
            BASE_ID, base_committed=True, slot_records=self.records_of(_record)
        )
        assert "drop_failed" in events(store)
        assert store.applied_record() is None
        reboot(mounter)
        mounter.fail_mount = mounter.fail_umount = False
        assert store.mount_at_boot(BASE_ID) is True
        assert mounter.mounted == {}

    def test_a_roll_back_whose_unmount_fails_puts_the_set_before_in_place(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        """The set put back is the applied one and what mounts at boot whatever
        fails after: the next start lays it, every slot of it."""
        _old = apply_set(store, PARAMS, LAUNCH)
        stage(store, PARAMS, LAUNCH, contents={"individual_params": "v2"})

        def _apply_fails_then_unmounts_do(command: str) -> None:
            runner.ran.append(command)
            mounter.fail_umount = True  # the roll back's, not the switch's
            raise RuntimeError(f"{command} failed")

        store._runner = _apply_fails_then_unmounts_do  # type: ignore[assignment]
        with pytest.raises(ParameterSetFailed) as _e:
            store.switch()
        store._runner = runner
        assert not _e.value.restored
        assert store.applied_record() == _old
        assert not store.previous_dir.exists()

        # the boot after: the boot-time unit lays the set put back
        mounter.fail_umount = False
        reboot(mounter)
        assert store.mount_at_boot(BASE_ID) is True
        for _slot in (PARAMS, LAUNCH):
            assert mounter.shows(_slot.path, store.tree_of(_slot))
        runner.ran.clear()
        assert (
            store.check_at_startup(
                BASE_ID, base_committed=True, slot_records=self.records_of(_old)
            )
            == _old
        )
        assert not store.discard_dir.exists()
        assert runner.ran == []

    def test_a_dropped_set_whose_unmount_fails_is_not_laid_again(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        _record = apply_set(store, PARAMS, LAUNCH)
        del mounter.mounted[LAUNCH.path]
        mounter.fail_mount = True
        mounter.fail_umount = True
        store.check_at_startup(
            BASE_ID, base_committed=True, slot_records=self.records_of(_record)
        )
        mounter.fail_mount = False
        assert "drop_failed" in events(store)
        assert store.applied_record() is None
        # the next start: nothing to lay
        mounter.fail_umount = False
        mounter.mounted.clear()
        runner.ran.clear()
        assert (
            store.check_at_startup(
                BASE_ID, base_committed=True, slot_records=self.records_of(_record)
            )
            is None
        )
        assert runner.ran == [] and mounter.mounted == {}

    def test_a_switch_recorded_but_not_committed_is_applied_after_a_reboot(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        """Power lost after the slot recorded the new release: the boot mounts
        nothing while previous/ is there, so what reads the slots started with the
        base's; the switch is completed and every slot of it applied again."""
        apply_set(store, PARAMS, REBOOT)
        _new = stage(store, PARAMS, REBOOT, content="v2")
        store.switch()
        reboot(mounter)
        runner.ran.clear()
        assert store.mount_at_boot(BASE_ID) is True
        assert mounter.mounted == {}

        assert (
            store.check_at_startup(
                BASE_ID, base_committed=True, slot_records=self.records_of(_new)
            )
            == _new
        )
        assert not store.previous_dir.exists()
        for _slot in (PARAMS, REBOOT):
            assert mounter.shows(_slot.path, store.tree_of(_slot))
        assert runner.ran == [PARAMS.apply]
        assert events(store)[-3:] == ["reboot_required", "recovered", "running"]

    def test_an_unfinished_switch_rolled_back_after_a_reboot_applies_every_slot(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        """The set put back fills LAUNCH with the files the failed set had there:
        by the file lists nothing changes back, but the boot mounted nothing, so it
        is applied too."""
        _old = apply_set(store, PARAMS, LAUNCH)
        stage(
            store,
            PARAMS,
            LAUNCH,
            content="v2",
            contents={PARAMS.name: "v2", LAUNCH.name: "v1"},
        )
        store.switch()
        reboot(mounter)
        runner.ran.clear()
        assert store.mount_at_boot(BASE_ID) is True

        assert (
            store.check_at_startup(
                BASE_ID, base_committed=True, slot_records=self.records_of(_old)
            )
            == _old
        )
        for _slot in (PARAMS, LAUNCH):
            assert mounter.shows(_slot.path, store.tree_of(_slot))
        assert sorted(runner.ran) == sorted([PARAMS.apply, LAUNCH.apply])

    def test_no_set_applied_leaves_the_mounts_alone(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        """Nothing otaclient keeps names a mount: with no set applied, nothing is
        unmounted."""
        mounter.mounted["/opt/elsewhere"] = (Path("/x"), 0)
        assert (
            store.check_at_startup(
                BASE_ID, base_committed=True, slot_records=FakeSlotRecords("", None)
            )
            is None
        )
        assert set(mounter.mounted) == {"/opt/elsewhere"}

    def test_a_roll_back_that_fails_is_not_recorded_as_recovered(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        _old = apply_set(store, PARAMS)
        stage(store, PARAMS, content="v2")
        store.switch()
        mounter.fail_umount = True
        store.check_at_startup(
            BASE_ID, base_committed=True, slot_records=self.records_of(_old)
        )
        assert "roll_back_failed" in events(store)
        assert "recovered" not in events(store)

    def test_an_apply_that_fails_after_a_mount_at_startup_keeps_the_set(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        """The slot did not mount at boot; mounted now, its apply fails: recorded,
        the set kept (a mount that fails drops it)."""
        _record = apply_set(store, PARAMS)
        mounter.mounted.clear()
        runner.fail_times = 1
        _records = self.records_of(_record)
        assert (
            store.check_at_startup(BASE_ID, base_committed=True, slot_records=_records)
            == _record
        )
        assert PARAMS.path in mounter.mounted
        assert "apply_failed" in events(store)
        assert _records.version == _record.firmware_version

    def test_a_set_of_another_image_is_named_in_the_history(
        self, store: ParameterSetStore
    ):
        _record = apply_set(store, PARAMS)
        store.check_at_startup(
            "another-image", base_committed=True, slot_records=self.records_of(_record)
        )
        _dropped = history(store)[-1]
        assert _dropped["event"] == "dropped"
        assert _dropped["set_manifest_digest"] == _record.set_manifest_digest
        assert _dropped["image_id"] == _record.image_id

    def test_a_link_to_a_directory_in_the_tree_is_found(
        self, store: ParameterSetStore, tmp_path: Path
    ):
        _record = apply_set(store, PARAMS)
        (tmp_path / "elsewhere").mkdir()
        (store.tree_of(PARAMS) / "linked").symlink_to(tmp_path / "elsewhere")
        assert "is not a directory" in (store.check_tree(_record) or "")


class TestHistory:
    def test_a_full_ota_carries_the_history_and_says_the_set_stays_behind(
        self, store: ParameterSetStore, tmp_path: Path, on_the_slots_own_root
    ):
        """The store is the slot's own: the new slot gets the history only."""
        _record = apply_set(store, PARAMS)
        _standby = tmp_path / "standby"
        (_standby / "opt").mkdir(mode=0o755, parents=True)

        store.carry_history_to(_standby, to_version="2.0.0")

        on_the_slots_own_root.assert_called_once_with(store.root)
        _store = _standby / "opt/data/ota/parameter-set"
        _events = [
            json.loads(_l) for _l in (_store / "history.jsonl").read_text().splitlines()
        ]
        assert [_e["event"] for _e in _events] == ["applied", "left_behind"]
        assert _events[-1]["set"] == _record.set_manifest_digest
        assert _events[-1]["reason"] == "full OTA to 2.0.0"
        assert _events[-1]["shared"] is False
        assert not (_store / "applied").exists()
        assert stat.S_IMODE(_store.stat().st_mode) == 0o700
        for _made in (_standby / "opt/data", _standby / "opt/data/ota"):
            assert stat.S_IMODE(_made.stat().st_mode) == 0o755
        assert events(store)[-1] == "applied", "the running slot's is not touched"
        assert not (store.root / "base-change-staged.json").exists()

    def test_a_link_on_the_way_in_the_new_slot_is_not_followed(
        self, store: ParameterSetStore, tmp_path: Path
    ):
        """The new image has /opt/data as a link: the history is not copied through
        it, wherever it points."""
        apply_set(store, PARAMS)
        _standby = tmp_path / "standby"
        (_standby / "opt").mkdir(mode=0o755, parents=True)
        (tmp_path / "elsewhere").mkdir(mode=0o755)
        (_standby / "opt/data").symlink_to(tmp_path / "elsewhere")

        store.carry_history_to(_standby, to_version="2.0.0")

        assert list((tmp_path / "elsewhere").iterdir()) == []

    def test_a_shared_store_stays_where_it_is_and_says_so(
        self, store: ParameterSetStore, tmp_path: Path, on_the_slots_own_root
    ):
        """On a partition both slots mount: nothing is copied, the line goes where
        the store is, and the new slot's otaclient drops the set by its base."""
        on_the_slots_own_root.return_value = False
        _record = apply_set(store, PARAMS)
        _standby = tmp_path / "standby"

        store.carry_history_to(_standby, to_version="2.0.0")

        assert not _standby.exists()
        # the new slot may run the same image: the mark says a full update was staged
        _staged = json.loads((store.root / "base-change-staged.json").read_text())
        assert _staged["to_version"] == "2.0.0"
        _left = history(store)[-1]
        assert _left["event"] == "left_behind"
        assert _left["shared"] is True
        assert _left["set"] == _record.set_manifest_digest
        assert store.applied_record() == _record, "dropped by the new slot, not here"
        assert (
            store.check_at_startup(
                "new-image",
                base_committed=True,
                slot_records=FakeSlotRecords("2.0.0", None),
            )
            is None
        )
        assert events(store)[-1] == "dropped"
        assert not (store.root / "base-change-staged.json").exists()

    def test_a_new_slot_written_as_an_image_keeps_the_set_where_it_is(
        self, store: ParameterSetStore
    ):
        """A partition-based layout: no root to copy into, the store stays."""
        apply_set(store, PARAMS)
        store.carry_history_to(None, to_version="3.0.0")
        assert history(store)[-1]["shared"] is True
        assert (store.root / "base-change-staged.json").exists()

    def test_a_slot_that_never_had_a_set_carries_nothing(
        self, store: ParameterSetStore, tmp_path: Path, on_the_slots_own_root
    ):
        store.carry_history_to(tmp_path / "standby", to_version="2.0.0")
        assert not (tmp_path / "standby").exists()
        assert not store.root.exists()

    def test_the_root_filesystem_is_the_hosts(
        self, tmp_path: Path, mocker: MockerFixture
    ):
        """From the nearest of the store and its parents that exists, against the
        running slot's root: the host's from an app image."""
        _host = tmp_path / "host_root"
        _host.mkdir(mode=0o755)
        _chroot = mocker.patch.object(
            parameter_set._env, "get_dynamic_client_chroot_path", return_value=None
        )
        _missing = tmp_path / "opt/data/ota/parameter-set"
        assert on_the_active_root_filesystem(_missing) is (
            os.stat(tmp_path).st_dev == os.stat("/").st_dev
        )
        _chroot.return_value = str(_host)
        assert on_the_active_root_filesystem(_missing) is True
        # another filesystem than the host's root
        _chroot.return_value = "/proc"
        assert on_the_active_root_filesystem(_missing) is False

    def test_the_history_rotates(self, store: ParameterSetStore):
        store.record_event("filler", blob="x" * (HISTORY_MAX_SIZE + 1))
        store.record_event("next")
        assert events(store) == ["next"]
        assert (store.root / "history.jsonl.1").exists()


class TestHostCommands:
    """What runs on the device, as the commands it is."""

    def test_mounts_go_to_the_hosts_namespace(self, mocker: MockerFixture):
        _run = mocker.patch.object(parameter_set, "subprocess_run_wrapper")
        mocker.patch.object(
            parameter_set._env, "is_dynamic_client_running", return_value=True
        )
        _mounter = HostParameterSlotMounter()
        _tree = "/opt/data/ota/parameter-set/applied/tree/opt/p"
        _mounter.bind_ro(Path(_tree), "/opt/p")
        _mounter.umount("/opt/p")
        assert [_c.args[0] for _c in _run.call_args_list] == [
            ["mount", "--bind", _tree, "/opt/p"],
            ["mount", "-o", "remount,bind,ro,nosuid,nodev", "/opt/p"],
            ["umount", "--lazy", "/opt/p"],
        ]
        assert all(_c.kwargs["set_host_mnt_ns"] for _c in _run.call_args_list)
        # bounded: the boot waits for them
        assert all(
            _c.kwargs["timeout"] == parameter_set.MOUNT_COMMAND_TIMEOUT
            for _c in _run.call_args_list
        )

    def test_a_slot_behind_a_link_is_not_mounted_over(
        self, tmp_path: Path, mocker: MockerFixture
    ):
        _run = mocker.patch.object(parameter_set, "subprocess_run_wrapper")
        (tmp_path / "real").mkdir()
        (tmp_path / "link").symlink_to(tmp_path / "real")
        with pytest.raises(OSError, match="symbolic link"):
            HostParameterSlotMounter().bind_ro(tmp_path / "src", str(tmp_path / "link"))
        _run.assert_not_called()

    def test_a_mount_point_the_host_lacks_is_made(self, tmp_path: Path):
        _host = tmp_path / "host_root"
        (_host / "opt").mkdir(parents=True)
        _mounter = HostParameterSlotMounter(str(_host))
        assert _mounter.make_mount_point("/opt/vehicle/overlay") is True
        assert (_host / "opt" / "vehicle" / "overlay").is_dir()
        assert stat.S_IMODE((_host / "opt/vehicle/overlay").stat().st_mode) == 0o755
        assert _mounter.make_mount_point("/opt/vehicle/overlay") is False

    def test_a_mount_point_is_made_with_0755_whatever_the_umask(self, tmp_path: Path):
        _host = tmp_path / "host_root"
        (_host / "opt").mkdir(parents=True)
        _umask = os.umask(0o077)
        try:
            HostParameterSlotMounter(str(_host)).make_mount_point("/opt/v/o")
        finally:
            os.umask(_umask)
        for _made in ("opt/v", "opt/v/o"):
            assert stat.S_IMODE((_host / _made).stat().st_mode) == 0o755

    def test_a_mount_point_leaves_the_mode_of_what_is_there(self, tmp_path: Path):
        _host = tmp_path / "host_root"
        (_host / "opt").mkdir(parents=True)
        (_host / "opt").chmod(0o750)
        HostParameterSlotMounter(str(_host)).make_mount_point("/opt/v")
        assert stat.S_IMODE((_host / "opt").stat().st_mode) == 0o750

    def test_a_mount_point_behind_a_link_or_a_file_is_not_made(self, tmp_path: Path):
        _host = tmp_path / "host_root"
        (_host / "opt").mkdir(parents=True)
        (_host / "elsewhere").mkdir()
        (_host / "opt" / "vehicle").symlink_to(_host / "elsewhere")
        _mounter = HostParameterSlotMounter(str(_host))
        (_host / "elsewhere").chmod(0o700)
        with pytest.raises(OSError, match="symbolic link"):
            _mounter.make_mount_point("/opt/vehicle/overlay")
        assert not (_host / "elsewhere" / "overlay").exists()
        assert stat.S_IMODE((_host / "elsewhere").stat().st_mode) == 0o700
        (_host / "opt" / "vehicle").unlink()
        (_host / "opt" / "vehicle").write_text("a file\n")
        with pytest.raises(OSError):
            _mounter.make_mount_point("/opt/vehicle/overlay")

    @pytest.mark.parametrize("dynamic", (True, False))
    def test_apply_runs_in_the_hosts_root_with_a_timeout(
        self, mocker: MockerFixture, dynamic: bool
    ):
        """From an app image, in the host's mount namespace (which is its root too):
        an apply the set carries is a file on a slot's mount, which the app image's
        own view may not have."""
        _run = mocker.patch.object(parameter_set, "subprocess_run_wrapper")
        mocker.patch.object(
            parameter_set._env, "is_dynamic_client_running", return_value=dynamic
        )
        run_apply("/opt/autoware/bin/apply-parameter-set")
        _run.assert_called_once()
        assert _run.call_args.args[0] == ["/opt/autoware/bin/apply-parameter-set"]
        assert "chroot" not in _run.call_args.kwargs
        assert _run.call_args.kwargs["set_host_mnt_ns"] is dynamic
        assert (
            _run.call_args.kwargs["timeout"]
            == parameter_set.cfg.PARAMETER_SET_APPLY_TIMEOUT
        )

    @pytest.mark.parametrize("found", (True, False))
    def test_is_mounted_asks_findmnt_in_the_hosts_namespace(
        self, mocker: MockerFixture, found: bool
    ):
        _run = mocker.patch.object(
            parameter_set,
            "subprocess_run_wrapper",
            side_effect=None if found else subprocess.CalledProcessError(1, "findmnt"),
        )
        mocker.patch.object(
            parameter_set._env, "is_dynamic_client_running", return_value=True
        )
        assert HostParameterSlotMounter().is_mounted("/opt/p") is found
        assert _run.call_args.args[0] == [
            "findmnt",
            "--noheadings",
            "--mountpoint",
            "/opt/p",
        ]
        assert _run.call_args.kwargs["set_host_mnt_ns"] is True

    def test_a_link_is_looked_for_on_the_host(
        self, tmp_path: Path, mocker: MockerFixture
    ):
        """From an app image, the slot's path is checked as the host sees it."""
        _run = mocker.patch.object(parameter_set, "subprocess_run_wrapper")
        _host = tmp_path / "host_root"
        (_host / "opt" / "real").mkdir(parents=True)
        (_host / "opt" / "p").symlink_to(_host / "opt" / "real")
        with pytest.raises(OSError, match="symbolic link"):
            HostParameterSlotMounter(str(_host)).bind_ro(tmp_path / "src", "/opt/p")
        _run.assert_not_called()
        # the same path, a real directory on the host, is mounted
        (_host / "opt" / "p").unlink()
        (_host / "opt" / "p").mkdir()
        HostParameterSlotMounter(str(_host)).bind_ro(tmp_path / "src", "/opt/p")
        assert _run.call_count == 2

    def test_a_bind_that_cannot_be_made_read_only_is_undone(
        self, mocker: MockerFixture
    ):
        def _run(cmd, **kwargs):
            if cmd[:2] == ["mount", "-o"]:
                raise subprocess.CalledProcessError(32, cmd, stderr=b"remount failed")

        _wrapper = mocker.patch.object(
            parameter_set, "subprocess_run_wrapper", side_effect=_run
        )
        with pytest.raises(CommandFailed, match="remount failed"):
            HostParameterSlotMounter("/").bind_ro(Path("/src"), "/opt/p")
        assert _wrapper.call_args.args[0] == ["umount", "--lazy", "/opt/p"]

    def test_a_failed_command_says_why(self, mocker: MockerFixture):
        mocker.patch.object(
            parameter_set,
            "subprocess_run_wrapper",
            side_effect=subprocess.CalledProcessError(
                1, ["/x"], stderr=b"x" * 5000 + b"Autoware did not come up\n"
            ),
        )
        with pytest.raises(CommandFailed) as _e:
            run_command(["/x"], what="apply /x")
        assert "apply /x failed with exit code 1" in str(_e.value)
        assert str(_e.value).endswith("Autoware did not come up")
        assert len(str(_e.value)) < 1200, "only the end of stderr"

        mocker.patch.object(
            parameter_set,
            "subprocess_run_wrapper",
            side_effect=subprocess.TimeoutExpired(["/x"], 300, stderr=b"waiting"),
        )
        with pytest.raises(CommandFailed, match="timed out after 300s: waiting"):
            run_command(["/x"], what="apply /x")

    def test_a_recorded_slot_is_otaclients_own(self):
        """The record keeps name, path and apply as they were checked; it reads back
        whatever the image library's slot model becomes."""
        _record = AppliedRecord(
            set_manifest_digest="sha256:" + "0" * 64,
            set_name="p",
            set_version="1",
            slots=[RecordedSlot.of(PARAMS)],
        )
        _raw = json.loads(_record.model_dump_json())
        assert _raw["slots"] == [PARAMS.model_dump()]
        assert AppliedRecord.model_validate_json(_record.model_dump_json()) == _record


class TestAtBoot:
    """`otaclient mount-parameter-set`, before local-fs.target."""

    def test_the_applied_set_is_bound_and_nothing_runs(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        _record = apply_set(store, PARAMS, LAUNCH)
        reboot(mounter)
        runner.ran.clear()

        assert store.mount_at_boot(BASE_ID) is True

        for _slot in (PARAMS, LAUNCH):
            assert mounter.shows(_slot.path, store.tree_of(_slot))
        assert runner.ran == [], "what reads the slots has not started yet"
        _event = history(store)[-1]
        assert _event["event"] == "mounted_at_boot"
        assert _event["set_manifest_digest"] == _record.set_manifest_digest
        assert _event["mounted"] == [PARAMS.name, LAUNCH.name]
        assert _event["failed"] == {}
        # otaclient's start finds every slot mounted: no apply, no remount
        assert (
            store.check_at_startup(
                BASE_ID,
                base_committed=True,
                slot_records=FakeSlotRecords(
                    _record.firmware_version,
                    VersionDetail(image_id=_record.image_id),
                ),
            )
            == _record
        )
        assert runner.ran == [] and "remounted" not in events(store)

    def test_one_event_whatever_was_done(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        """A mount point made and a slot already mounted are in the one event."""
        apply_set(store, PARAMS, LAUNCH)
        reboot(mounter)
        mounter.missing.add(LAUNCH.path)
        mounter.bind_ro(store.tree_of(PARAMS), PARAMS.path)
        _before = len(events(store))

        assert store.mount_at_boot(BASE_ID) is True

        assert events(store)[_before:] == ["mounted_at_boot"]
        _event = history(store)[-1]
        assert _event["mounted"] == [LAUNCH.name]
        assert _event["already_mounted"] == [PARAMS.name]
        assert _event["mount_points_made"] == [LAUNCH.path]

    def test_a_set_of_another_image_is_not_mounted(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        """This boot may be the trial of a new base: the store is left as it is,
        not even a line in the history; otaclient drops the set once the base is
        committed."""
        _record = apply_set(store, PARAMS)
        reboot(mounter)
        runner.ran.clear()
        _history = store.history_file.read_text()

        assert store.mount_at_boot("another-image") is True

        assert mounter.mounted == {}
        assert store.history_file.read_text() == _history
        assert store.applied_record() == _record, "not dropped here"
        assert runner.ran == []

    def test_an_unfinished_switch_is_not_mounted(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        """Whether it completes or rolls back takes the slot's records, which
        otaclient's start has."""
        apply_set(store, PARAMS)
        stage(store, PARAMS, content="v2")
        store.switch()
        reboot(mounter)
        runner.ran.clear()

        assert store.mount_at_boot(BASE_ID) is True

        assert mounter.mounted == {}
        assert store.previous_dir.exists(), "left to otaclient"
        assert history(store)[-1]["event"] == "not_mounted_at_boot"
        assert runner.ran == []

    def test_a_slot_that_cannot_be_mounted_fails_and_the_others_are_mounted(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        apply_set(store, PARAMS, LAUNCH)
        reboot(mounter)
        runner.ran.clear()
        _bind = mounter.bind_ro

        def _params_fails(source: Path, mount_point: str) -> None:
            if mount_point == PARAMS.path:
                raise OSError("no such directory")
            _bind(source, mount_point)

        mounter.bind_ro = _params_fails  # type: ignore[method-assign]

        assert store.mount_at_boot(BASE_ID) is False

        assert set(mounter.mounted) == {LAUNCH.path}
        _event = history(store)[-1]
        assert _event["mounted"] == [LAUNCH.name]
        assert "no such directory" in _event["failed"][PARAMS.name]
        assert runner.ran == []

    def test_nothing_applied_is_nothing_to_do_and_nothing_recorded(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        assert store.mount_at_boot(BASE_ID) is True
        assert mounter.mounted == {}
        assert not store.root.exists(), "the boot makes no store"

    def test_an_unreadable_record_mounts_nothing_and_fails(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        """Whose set it is cannot be told: logged, the store left as it is."""
        apply_set(store, PARAMS)
        reboot(mounter)
        (store.applied_dir / "record.json").write_text("{")
        _history = store.history_file.read_text()
        assert store.mount_at_boot(BASE_ID) is False
        assert mounter.mounted == {}
        assert store.history_file.read_text() == _history


class TestTheCommand:
    """`otaclient mount-parameter-set`, as the app image's boot-time unit runs it."""

    @pytest.fixture(autouse=True)
    def no_logging_setup(self, mocker: MockerFixture):
        mocker.patch.object(parameter_set.logging, "basicConfig")

    @pytest.fixture
    def app_image(self, tmp_path: Path, mocker: MockerFixture) -> Path:
        """An app image: the host's root at its chroot path, mounts through the
        host's mount namespace. The store is the host's, at the path the unit binds
        /opt to: here, the host's own path in the test's tree."""
        _host = tmp_path / "host_root"
        (_host / "usr/share/ota").mkdir(mode=0o755, parents=True)
        (_host / "usr/share/ota/version.json").write_text(
            json.dumps({"schema_version": 1, "image_id": BASE_ID})
        )
        mocker.patch.object(
            parameter_set._env, "is_dynamic_client_running", return_value=True
        )
        mocker.patch.object(
            parameter_set._env,
            "get_dynamic_client_chroot_path",
            return_value=str(_host),
        )
        mocker.patch.object(
            parameter_set.cfg,
            "PARAMETER_SET_DPATH",
            str(_host / "opt/data/ota/parameter-set"),
        )
        return _host

    @staticmethod
    def applied_on_the_host(host: Path) -> ParameterSetStore:
        _store = ParameterSetStore(
            root=host / "opt/data/ota/parameter-set",
            mounter=FakeMounter(),
            runner=FakeRunner(),
        )
        apply_set(_store, PARAMS)
        return _store

    def test_the_set_is_mounted_in_the_hosts_namespace(
        self, tmp_path: Path, app_image: Path, mocker: MockerFixture
    ):
        """What it reads is the host's (the version file, the slot's path), what it
        runs goes to the host's mount namespace, bounded, and no apply runs."""
        _store = self.applied_on_the_host(app_image)
        _ran: list[list[str]] = []

        def _run(cmd, **kwargs):
            assert kwargs["set_host_mnt_ns"] is True
            assert kwargs["timeout"] == parameter_set.MOUNT_COMMAND_TIMEOUT
            assert "chroot" not in kwargs
            _ran.append(cmd)
            if cmd[0] == "findmnt":
                raise subprocess.CalledProcessError(1, cmd)

        mocker.patch.object(parameter_set, "subprocess_run_wrapper", side_effect=_run)

        assert parameter_set.mount_parameter_set_command() == 0

        _tree = str(_store.tree_of(PARAMS))
        assert _ran == [
            ["findmnt", "--noheadings", "--mountpoint", PARAMS.path],
            ["mount", "--bind", _tree, PARAMS.path],
            ["mount", "-o", "remount,bind,ro,nosuid,nodev", PARAMS.path],
        ]
        # the mount point was made on the host
        assert (app_image / PARAMS.relpath).is_dir()

    def test_a_store_others_can_change_lays_nothing_and_fails(
        self, app_image: Path, mocker: MockerFixture
    ):
        _store = self.applied_on_the_host(app_image)
        _store.root.parent.chmod(0o777)
        _run = mocker.patch.object(parameter_set, "subprocess_run_wrapper")
        assert parameter_set.mount_parameter_set_command() == 1
        _run.assert_not_called()

    def test_a_set_of_another_image_mounts_nothing(
        self, app_image: Path, mocker: MockerFixture
    ):
        (app_image / "usr/share/ota/version.json").write_text(
            json.dumps({"schema_version": 1, "image_id": "another-image"})
        )
        self.applied_on_the_host(app_image)
        _run = mocker.patch.object(parameter_set, "subprocess_run_wrapper")
        assert parameter_set.mount_parameter_set_command() == 0
        _run.assert_not_called()

    @pytest.mark.parametrize("mounted, code", ((True, 0), (False, 1)))
    def test_the_exit_code_says_whether_every_slot_is_mounted(
        self, app_image: Path, mocker: MockerFixture, mounted: bool, code: int
    ):
        _store = mocker.MagicMock()
        _store.mount_at_boot.return_value = mounted
        mocker.patch.object(parameter_set, "ParameterSetStore", return_value=_store)
        assert parameter_set.mount_parameter_set_command() == code
        _store.mount_at_boot.assert_called_once_with(BASE_ID)

    def test_an_error_is_exit_1_not_a_hang_or_a_raise(
        self, app_image: Path, mocker: MockerFixture
    ):
        mocker.patch.object(
            parameter_set, "ParameterSetStore", side_effect=OSError("I/O error")
        )
        assert parameter_set.mount_parameter_set_command() == 1

    @pytest.mark.parametrize("code", (0, 1))
    def test_the_subcommand_of_the_entry_point(self, mocker: MockerFixture, code: int):
        """`/otaclient/otaclient mount-parameter-set` (`python3 -m otaclient
        mount-parameter-set`): only the command, not otaclient itself."""
        _command = mocker.patch.object(
            parameter_set, "mount_parameter_set_command", return_value=code
        )
        _main = mocker.patch("otaclient.main.main")
        mocker.patch.object(sys, "argv", ["otaclient", "mount-parameter-set"])
        with pytest.raises(SystemExit) as _exit:
            runpy.run_module("otaclient", run_name="__main__")
        assert _exit.value.code == code
        _command.assert_called_once_with()
        _main.assert_not_called()


class TestAnotherBasesSetInTheSlotsOwnStore:
    """A store on the slot's own root that holds the set of another image (its
    version file changed in place): no other slot can lay it, so it is dropped
    whatever the status."""

    def test_dropped_also_when_not_committed(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        _record = apply_set(store, PARAMS, base_image_id="image-a")
        reboot(mounter)
        runner.ran.clear()
        assert store.mount_at_boot("image-b") is True
        assert mounter.mounted == {}
        assert (
            store.check_at_startup(
                "image-b",
                slot_records=FakeSlotRecords("2.0.0", None),
                base_committed=False,
            )
            is None
        )
        assert store.applied_record() is None
        assert mounter.mounted == {} and runner.ran == []
        _last = history(store)[-1]
        assert _last["event"] == "dropped"
        assert _last["set_manifest_digest"] == _record.set_manifest_digest


class TestAnotherBasesSet:
    """A store the slots share holds the set base A applied while base B runs: the
    trial boot of B after a full update, which may still fall back to A."""

    A, B = "image-a", "image-b"

    @pytest.fixture
    def shared(self, on_the_slots_own_root) -> None:
        on_the_slots_own_root.return_value = False

    @pytest.fixture
    def on_a(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner, shared
    ) -> AppliedRecord:
        """A's set, applied on A; then the reboot into B."""
        _record = apply_set(store, PARAMS, base_image_id=self.A)
        reboot(mounter)
        runner.ran.clear()
        return _record

    def records_of(self, record: AppliedRecord) -> FakeSlotRecords:
        return FakeSlotRecords(
            record.firmware_version, VersionDetail(image_id=record.image_id)
        )

    def test_a_trial_not_committed_keeps_the_set(
        self,
        store: ParameterSetStore,
        mounter: FakeMounter,
        runner: FakeRunner,
        on_a: AppliedRecord,
    ):
        """Neither the boot nor otaclient's start lays it on B; the store is kept as
        it is, said once per boot."""
        assert store.mount_at_boot(self.B) is True
        _records = FakeSlotRecords("2.0.0", None)
        for _ in range(2):  # otaclient restarts
            assert (
                store.check_at_startup(
                    self.B, slot_records=_records, base_committed=False
                )
                is None
            )
        assert mounter.mounted == {} and runner.ran == []
        assert store.applied_record() == on_a
        assert events(store).count("not_laid") == 1
        assert history(store)[-1]["set_manifest_digest"] == on_a.set_manifest_digest
        assert _records.version == "2.0.0"

    def test_the_fallback_base_lays_it_again(
        self,
        store: ParameterSetStore,
        mounter: FakeMounter,
        runner: FakeRunner,
        on_a: AppliedRecord,
    ):
        """The trial of B was not committed and the platform went back to A: the IDs
        match again, and A's set is laid as on any boot."""
        store.check_at_startup(
            self.B, slot_records=FakeSlotRecords("2.0.0", None), base_committed=False
        )
        reboot(mounter)

        assert store.mount_at_boot(self.A) is True
        # A booted at FAILURE (its update did not finish): its own set all the same
        assert (
            store.check_at_startup(
                self.A, slot_records=self.records_of(on_a), base_committed=False
            )
            == on_a
        )
        assert mounter.shows(PARAMS.path, store.tree_of(PARAMS))
        assert runner.ran == [], "laid at boot, before what reads it"

    def test_a_committed_base_drops_it(
        self,
        store: ParameterSetStore,
        mounter: FakeMounter,
        runner: FakeRunner,
        on_a: AppliedRecord,
    ):
        _records = FakeSlotRecords("2.0.0", VersionDetail(image_id=self.B))
        assert (
            store.check_at_startup(self.B, slot_records=_records, base_committed=True)
            is None
        )
        assert store.applied_record() is None
        assert mounter.mounted == {} and runner.ran == []
        assert events(store)[-1] == "dropped"
        assert _records.version == "2.0.0", "B's records are B's"

    def test_a_switch_a_did_not_finish_is_not_bs_to_finish(
        self,
        store: ParameterSetStore,
        mounter: FakeMounter,
        runner: FakeRunner,
        shared,
    ):
        """previous/ of A's: not completed nor rolled back with B's records; kept
        until B is committed, then dropped with the rest."""
        apply_set(store, PARAMS, base_image_id=self.A)
        stage(store, PARAMS, content="v2", base_image_id=self.A)
        store.switch()
        reboot(mounter)
        runner.ran.clear()
        _records = FakeSlotRecords("2.0.0", None)

        assert store.mount_at_boot(self.B) is True
        store.check_at_startup(self.B, slot_records=_records, base_committed=False)
        assert store.previous_dir.exists() and store.applied_dir.exists()
        assert mounter.mounted == {} and runner.ran == []

        store.check_at_startup(self.B, slot_records=_records, base_committed=True)
        assert not store.previous_dir.exists() and not store.applied_dir.exists()
        assert mounter.mounted == {} and runner.ran == []

    def test_an_unreadable_record_is_dropped_only_once_committed(
        self, store: ParameterSetStore, mounter: FakeMounter, on_a: AppliedRecord
    ):
        (store.applied_dir / "record.json").write_text("{")
        _records = FakeSlotRecords("2.0.0", None)
        store.check_at_startup(self.B, slot_records=_records, base_committed=False)
        assert store.applied_dir.exists()
        store.check_at_startup(self.B, slot_records=_records, base_committed=True)
        assert not store.applied_dir.exists()

    def test_a_set_on_a_base_without_an_id_is_never_laid_from_a_shared_store(
        self,
        store: ParameterSetStore,
        mounter: FakeMounter,
        runner: FakeRunner,
        shared,
    ):
        """ "" is every such base's ID: A's set would be laid on B. Such a record is
        not made any more (the update refuses it); one found is not laid."""
        _record = apply_set(store, PARAMS, base_image_id="")
        reboot(mounter)
        runner.ran.clear()

        assert store.mount_at_boot("") is True
        assert mounter.mounted == {}
        _records = self.records_of(_record)
        store.check_at_startup("", slot_records=_records, base_committed=False)
        assert store.applied_record() == _record and mounter.mounted == {}
        store.check_at_startup("", slot_records=_records, base_committed=True)
        assert store.applied_record() is None and mounter.mounted == {}
        assert runner.ran == []
        assert "without an image ID" in history(store)[-1]["reason"]

    def test_a_set_of_another_base_is_replaced_not_the_set_before(
        self,
        store: ParameterSetStore,
        mounter: FakeMounter,
        runner: FakeRunner,
        on_a: AppliedRecord,
    ):
        """A new set on B: every slot of it is applied (nothing compared with A's),
        and when it fails, B goes back to its defaults, not to A's set."""
        store.set_aside_another_bases_set(
            self.B, slot_records=FakeSlotRecords("2.0.0", None)
        )
        assert store.applied_record() is None
        assert events(store)[-1] == "dropped"

        stage(store, PARAMS, base_image_id=self.B)  # the same files as A's
        runner.fail_times = 1
        with pytest.raises(ParameterSetFailed) as _e:
            store.switch()
        assert _e.value.restored
        assert runner.ran == [PARAMS.apply], "applied: not compared with A's"
        assert store.applied_record() is None and mounter.mounted == {}

    def test_the_running_bases_set_is_the_set_before(
        self, store: ParameterSetStore, shared
    ):
        _record = apply_set(store, PARAMS, base_image_id=self.B)
        store.set_aside_another_bases_set(self.B, slot_records=self.records_of(_record))
        assert store.applied_record() == _record

    def test_an_unreadable_set_is_not_the_set_before_either(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner, shared
    ):
        """Whose it is cannot be told: set aside, and a new set that fails goes back
        to the base's defaults."""
        apply_set(store, PARAMS, base_image_id=self.B)
        (store.applied_dir / "record.json").write_text("{")
        reboot(mounter)
        runner.ran.clear()
        store.set_aside_another_bases_set(
            self.B, slot_records=FakeSlotRecords("2.0.0", None)
        )
        assert not store.applied_dir.exists()
        assert "unreadable" in history(store)[-1]["reason"]

        stage(store, PARAMS, content="v2", base_image_id=self.B)
        runner.fail_times = 1
        with pytest.raises(ParameterSetFailed) as _e:
            store.switch()
        assert _e.value.restored
        assert runner.ran == [PARAMS.apply]
        assert store.applied_record() is None and mounter.mounted == {}

    def test_a_newer_schema_is_not_laid_at_boot(
        self, store: ParameterSetStore, mounter: FakeMounter, shared
    ):
        """Its fields may mean what this otaclient does not know: left to the start
        of the otaclient that runs."""
        _record = apply_set(store, PARAMS, base_image_id=self.B)
        reboot(mounter)
        (store.applied_dir / "record.json").write_text(
            _record.model_copy(update={"schema_version": 2}).model_dump_json()
        )
        assert store.mount_at_boot(self.B) is True
        assert mounter.mounted == {}


class TestTheSameImageAgain:
    """A full update installs, in the other slot, the very image a set in a store the
    slots share was applied on (the base release delivered again, to clear the set):
    the image ID cannot tell the two slots apart, the mark of the staged full update
    and the slot's records do."""

    X = "image-x"

    @pytest.fixture
    def staged(
        self,
        store: ParameterSetStore,
        mounter: FakeMounter,
        runner: FakeRunner,
        on_the_slots_own_root,
    ) -> AppliedRecord:
        """S applied on slot A over X; a full update to X staged from A; the reboot."""
        on_the_slots_own_root.return_value = False
        _record = apply_set(store, PARAMS, base_image_id=self.X)
        store.carry_history_to(None, to_version="2.0.0")
        reboot(mounter)
        runner.ran.clear()
        return _record

    @staticmethod
    def new_slots_records() -> FakeSlotRecords:
        return FakeSlotRecords("2.0.0", VersionDetail(image_id="image-x"))

    @staticmethod
    def records_of(record: AppliedRecord) -> FakeSlotRecords:
        return FakeSlotRecords(
            record.firmware_version, VersionDetail(image_id=record.image_id)
        )

    def test_the_boot_lays_nothing_while_a_full_update_is_staged(
        self, store: ParameterSetStore, mounter: FakeMounter, staged: AppliedRecord
    ):
        _history = store.history_file.read_text()
        assert store.mount_at_boot(self.X) is True
        assert mounter.mounted == {}
        assert store.history_file.read_text() == _history

    def test_the_new_slot_committed_drops_it(
        self,
        store: ParameterSetStore,
        mounter: FakeMounter,
        runner: FakeRunner,
        staged: AppliedRecord,
    ):
        _records = self.new_slots_records()
        assert (
            store.check_at_startup(self.X, slot_records=_records, base_committed=True)
            is None
        )
        assert store.applied_record() is None and mounter.mounted == {}
        assert not store.base_change_staged()
        assert runner.ran == []
        assert _records.version == "2.0.0", "the new slot reports its own base"
        assert "this slot does not record" in history(store)[-1]["reason"]

    def test_the_new_slot_not_committed_keeps_it_and_the_mark(
        self, store: ParameterSetStore, mounter: FakeMounter, staged: AppliedRecord
    ):
        assert (
            store.check_at_startup(
                self.X, slot_records=self.new_slots_records(), base_committed=False
            )
            is None
        )
        assert store.applied_record() == staged and mounter.mounted == {}
        assert store.base_change_staged()

    def test_the_slot_it_was_applied_on_lays_and_applies_it(
        self,
        store: ParameterSetStore,
        mounter: FakeMounter,
        runner: FakeRunner,
        staged: AppliedRecord,
    ):
        """A fell back (or never switched): it still records S's release. The boot
        laid nothing, so what reads the slots runs with the defaults: S is laid and
        its slots applied, and the mark goes."""
        assert store.mount_at_boot(self.X) is True
        assert (
            store.check_at_startup(
                self.X, slot_records=self.records_of(staged), base_committed=False
            )
            == staged
        )
        assert mounter.shows(PARAMS.path, store.tree_of(PARAMS))
        assert runner.ran == [PARAMS.apply]
        assert not store.base_change_staged()
        assert "base_change_not_taken" in events(store)
        # the next boot lays it again, as any
        reboot(mounter)
        assert store.mount_at_boot(self.X) is True
        assert mounter.shows(PARAMS.path, store.tree_of(PARAMS))


class TestTheStoreIsRoots:
    """Every directory from the mount point the store is on (tmp_path here) down to
    the store is root's alone."""

    @pytest.mark.parametrize(
        "spoil, match",
        (
            pytest.param(lambda d: d.chmod(0o777), "mode 777", id="others_can_write"),
            pytest.param(
                lambda d: d.chmod(0o775), "mode 775", id="the_group_can_write"
            ),
            pytest.param(
                lambda d: os.chown(d, 1000, 1000),
                "owner uid 1000",
                id="not_roots",
                marks=pytest.mark.skipif(
                    os.geteuid() != 0, reason="giving a directory away takes root"
                ),
            ),
        ),
    )
    def test_a_directory_on_the_way_another_user_can_change(
        self, tmp_path: Path, spoil, match: str
    ):
        _store = tmp_path / "opt/data/ota/parameter-set"
        _io.ensure_private_dir(_store, mode=0o700)
        assert why_not_private(_store) is None, "as made: root's"
        spoil(tmp_path / "opt/data")
        _why = why_not_private(_store)
        assert _why is not None and match in _why
        assert str(tmp_path / "opt/data") in _why

    def test_a_link_on_the_way(self, tmp_path: Path):
        (tmp_path / "elsewhere/ota").mkdir(mode=0o755, parents=True)
        (tmp_path / "opt").mkdir(mode=0o755)
        (tmp_path / "opt/data").symlink_to(tmp_path / "elsewhere")
        _why = why_not_private(tmp_path / "opt/data/ota/parameter-set")
        assert _why == f"{tmp_path / 'opt/data'} is a symlink"

    def test_what_is_not_there_yet_is_not_looked_at(self, tmp_path: Path):
        (tmp_path / "opt").mkdir(mode=0o755)
        assert why_not_private(tmp_path / "opt/data/ota/x") is None

    def test_what_otaclient_makes_is_made_with_its_modes(self, tmp_path: Path):
        """/opt/data and /opt/data/ota 0755, the store 0700, whatever the umask."""
        (tmp_path / "opt").mkdir(mode=0o750)
        _umask = os.umask(0o077)
        try:
            ParameterSetStore(
                root=tmp_path / "opt/data/ota/parameter-set"
            ).record_event("made")
        finally:
            os.umask(_umask)
        assert (
            stat.S_IMODE((tmp_path / "opt/data/ota/parameter-set").stat().st_mode)
            == 0o700
        )
        for _made in ("opt/data", "opt/data/ota"):
            assert stat.S_IMODE((tmp_path / _made).stat().st_mode) == 0o755
        assert stat.S_IMODE((tmp_path / "opt").stat().st_mode) == 0o750

    def test_the_release_of_a_set_not_laid_is_not_reported(
        self, store: ParameterSetStore, mocker: MockerFixture
    ):
        """The slot reports its base, not the release of a set it does not lay."""
        _record = apply_set(store, PARAMS)
        mocker.patch.object(
            _image_version_file, "ParameterSetStore", return_value=store
        )
        _recorded = VersionDetail(release_name="r", image_id=_record.image_id)
        assert _image_version_file._release_of_the_applied_parameter_set(
            _recorded, BASE_ID
        )
        store.root.parent.chmod(0o777)
        assert not _image_version_file._release_of_the_applied_parameter_set(
            _recorded, BASE_ID
        )

    @pytest.fixture
    def planted(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ) -> Path:
        """A set applied, then the directory holding the store opened to others."""
        apply_set(store, PARAMS)
        reboot(mounter)
        runner.ran.clear()
        store.root.parent.chmod(0o777)
        return store.root.parent

    def test_nothing_is_laid_at_boot(
        self, store: ParameterSetStore, mounter: FakeMounter, planted: Path
    ):
        _history = store.history_file.read_text()
        assert store.mount_at_boot(BASE_ID) is False
        assert mounter.mounted == {}
        assert store.history_file.read_text() == _history, "nothing recorded"

    def test_nothing_is_laid_nor_deleted_at_startup(
        self,
        store: ParameterSetStore,
        mounter: FakeMounter,
        runner: FakeRunner,
        planted: Path,
    ):
        _history = store.history_file.read_text()
        for _image, _committed in ((BASE_ID, True), ("another-image", True)):
            assert (
                store.check_at_startup(
                    _image,
                    slot_records=FakeSlotRecords("", None),
                    base_committed=_committed,
                )
                is None
            )
        assert mounter.mounted == {} and runner.ran == []
        assert store.applied_dir.exists(), "not deleted"
        assert store.history_file.read_text() == _history
