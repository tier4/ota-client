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
"""The parameter set store: the switch, fstab, the check at startup and the history."""

from __future__ import annotations

import json
import os
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest
from ota_image_libs.common.oci_spec import Sha256Digest
from ota_image_libs.v1.parameter_set.schema import ParameterSetFileEntry
from ota_image_libs.v1.parameter_set.slots import ParameterSlot
from pytest_mock import MockerFixture

from otaclient import parameter_set
from otaclient._types import VersionDetail
from otaclient.parameter_set import (
    FSTAB_BEGIN,
    FSTAB_END,
    HISTORY_MAX_SIZE,
    TREE_DNAME,
    AppliedRecord,
    CommandFailed,
    HostParameterSlotMounter,
    ParameterSetFailed,
    ParameterSetStore,
    RecordedSlot,
    run_apply,
    run_command,
)

PARAMS = ParameterSlot(
    name="individual_params",
    path="/opt/autoware/vehicle/vehicle.j6_gen2/individual_params",
    apply="/opt/autoware/bin/apply-parameter-set",
)
LAUNCH = ParameterSlot(
    name="launch",
    path="/opt/autoware/vehicle/vehicle.j6_gen2/launch",
    apply="/opt/autoware/bin/apply-launch",
)
REBOOT = ParameterSlot(name="kernel", path="/opt/kernel-params", apply="reboot")
BASE_ID = "base-image"
FSTAB = "UUID=abc\t/\text4\terrors=remount-ro\t0\t1\n"
BASE_DETAIL = VersionDetail(release_name="base", release_id="b", image_id="base-image")


class FakeMounter:
    """Mounts as the kernel keeps them: a bind is of the directory it was made from
    (its inode), whatever that directory is renamed to later."""

    def __init__(self) -> None:
        self.mounted: dict[str, tuple[Path, int]] = {}
        self.fail_mount = False
        self.fail_umount: bool | set[str] = False

    def is_mounted(self, mount_point: str) -> bool:
        return mount_point in self.mounted

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
            raise OSError("target is busy")
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
    _fstab = tmp_path / "fstab"
    _fstab.write_text(FSTAB)
    return ParameterSetStore(
        root=tmp_path / "store", fstab=_fstab, mounter=mounter, runner=runner
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


def fstab_block(store: ParameterSetStore) -> list[str]:
    _lines = store.fstab.read_text().splitlines()
    if FSTAB_BEGIN not in _lines:
        return []
    return _lines[_lines.index(FSTAB_BEGIN) + 1 : _lines.index(FSTAB_END)]


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
        _block = fstab_block(store)
        assert len(_block) == 2
        # read-only, a slot may hold executables (none of them privileged), a slot
        #   that cannot be mounted does not stop the boot, and it is mounted before
        #   what reads it starts
        assert _block[0].split("\t") == [
            str(store.applied_dir / TREE_DNAME / PARAMS.relpath),
            PARAMS.path,
            "none",
            "bind,ro,nosuid,nodev,nofail,x-systemd.before=local-fs.target",
            "0",
            "0",
        ]
        assert store.fstab.read_text().startswith(FSTAB), "the rest of fstab is kept"
        assert runner.ran == [PARAMS.apply, LAUNCH.apply]
        _event = history(store)[-1]
        assert _event["event"] == "applied"
        assert _event["image_id"] == "parameter-image-v1"
        assert _event["base_image_id"] == "base-image"

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

    def test_a_new_set_without_a_slot_gives_it_back_the_base_defaults(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        apply_set(store, PARAMS, LAUNCH)
        runner.ran.clear()
        apply_set(store, PARAMS)

        assert set(mounter.mounted) == {PARAMS.path}
        assert len(fstab_block(store)) == 1
        assert store.fstab.read_text().count(FSTAB_BEGIN) == 1
        assert runner.ran == [LAUNCH.apply]

    def test_an_empty_set_gives_every_slot_the_base_defaults(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        apply_set(store, PARAMS)
        _empty = apply_set(store, content="v2")

        assert mounter.mounted == {}
        assert fstab_block(store) == []
        assert store.fstab.read_text() == FSTAB
        assert runner.ran == [PARAMS.apply, PARAMS.apply], "the slot changed back"
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
        assert len(fstab_block(store)) == 1
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
        assert fstab_block(store) == []
        assert mounter.mounted == {}

    def test_a_busy_slot_leaves_the_applied_set_where_it_was(
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
        assert len(fstab_block(store)) == 1

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

    def test_paths_with_spaces_are_escaped_in_fstab(self, store: ParameterSetStore):
        _spaced = ParameterSlot(name="s", path="/opt/a b", apply="reboot")
        stage(store, _spaced)
        store.switch()
        assert "/opt/a\\040b" in fstab_block(store)[0]

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
        the old apply, the new one is filled with the new apply."""
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
        assert sorted(runner.ran) == ["/usr/bin/reload-new", "/usr/bin/reload-old"]

    def test_another_apply_for_the_same_files_is_a_change(
        self, store: ParameterSetStore, runner: FakeRunner
    ):
        apply_set(store, PARAMS)
        runner.ran.clear()
        _moved = ParameterSlot(name=PARAMS.name, path=PARAMS.path, apply="/usr/bin/x")
        apply_set(store, _moved)
        assert runner.ran == ["/usr/bin/x"]

    def test_a_partly_busy_unmount_mounts_back_what_came_off(
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
        assert store.check_at_startup(BASE_ID, slot_records=_records) == _record
        assert store.check_at_startup(BASE_ID, slot_records=_records) == _record
        assert events(store).count("running") == 1, "a restart adds no line"
        boot("boot-2")
        store.check_at_startup(BASE_ID, slot_records=_records)
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

        assert store.check_at_startup(BASE_ID, slot_records=_records) == _record
        assert _records.version == _record.firmware_version
        assert _records.detail and _records.detail.image_id == _record.image_id
        assert PARAMS.path in mounter.mounted and len(fstab_block(store)) == 1
        assert _f.read_text() == "tampered\n", "not put back either"
        assert runner.ran == []
        _events = history(store)
        assert [_e["event"] for _e in _events[-2:]] == ["running", "tree_mismatch"]
        assert "differs" in _events[-1]["reason"]

        store.check_at_startup(BASE_ID, slot_records=_records)
        assert events(store).count("tree_mismatch") == 1, "a restart adds no line"
        boot("boot-2")
        store.check_at_startup(BASE_ID, slot_records=_records)
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
            store.check_at_startup(BASE_ID, slot_records=self.records_of(_record))
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
        assert store.check_at_startup(BASE_ID, slot_records=_records) is None
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
        """A set copied in with the slot: the slot's records are the new image's."""
        _record = apply_set(store, PARAMS, base_image_id=applied_on)
        runner.ran.clear()
        _records = self.records_of(_record)
        assert store.check_at_startup(running, slot_records=_records) is None
        assert _records.version == _record.firmware_version
        assert store.applied_record() is None and fstab_block(store) == []
        assert mounter.mounted == {}
        assert runner.ran == []
        assert "was applied on image" in history(store)[-1]["reason"]

    def test_a_set_applied_on_an_image_without_a_version_file_stays(
        self, store: ParameterSetStore
    ):
        _record = apply_set(store, PARAMS, base_image_id="")
        assert (
            store.check_at_startup("", slot_records=self.records_of(_record)) == _record
        )
        assert "tree_mismatch" not in events(store)

    def test_an_unreadable_record_drops_the_set(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        """What it mounted is told by otaclient's block of fstab."""
        _spaced = ParameterSlot(name="s", path="/opt/a b", apply="reboot")
        apply_set(store, PARAMS, _spaced)
        assert set(mounter.mounted) == {PARAMS.path, "/opt/a b"}
        (store.applied_dir / "record.json").write_text("")
        _records = FakeSlotRecords("x", None)
        assert store.check_at_startup(BASE_ID, slot_records=_records) is None
        assert not store.applied_dir.exists()
        assert mounter.mounted == {} and fstab_block(store) == []
        assert "unreadable" in history(store)[-1]["reason"]

    def test_an_unfinished_switch_is_rolled_back(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        """Power lost after the switch, before the release was recorded."""
        _old = apply_set(store, PARAMS)
        stage(store, PARAMS, content="v2")
        store.switch()

        assert (
            store.check_at_startup(BASE_ID, slot_records=self.records_of(_old)) == _old
        )
        assert not store.previous_dir.exists()
        assert events(store)[-3:] == ["rolled_back", "recovered", "running"]

    def test_a_drop_whose_apply_fails_is_recorded_as_such(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        _record = apply_set(store, PARAMS)
        del mounter.mounted[PARAMS.path]
        mounter.fail_mount = True
        runner.fail_times = 1
        _records = self.records_of(_record)
        store.check_at_startup(BASE_ID, slot_records=_records)
        # dropped all the same: the slot reports the base and holds its defaults
        assert events(store)[-2:] == ["dropped", "apply_failed"]
        assert store.applied_record() is None and fstab_block(store) == []
        assert _records.version == "1.0.0", "the base's records come back first"

    def test_a_slot_that_did_not_mount_at_boot_is_mounted_and_applied(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        _record = apply_set(store, PARAMS, REBOOT)
        mounter.mounted.clear()
        runner.ran.clear()

        assert (
            store.check_at_startup(BASE_ID, slot_records=self.records_of(_record))
            == _record
        )
        assert PARAMS.path in mounter.mounted
        assert runner.ran == [PARAMS.apply]
        assert events(store)[-3:] == ["remounted", "reboot_required", "running"]

    def test_a_slot_that_cannot_be_mounted_drops_the_set(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        _record = apply_set(store, PARAMS, LAUNCH)
        del mounter.mounted[LAUNCH.path]
        mounter.fail_mount = True
        _records = self.records_of(_record)
        assert store.check_at_startup(BASE_ID, slot_records=_records) is None
        assert mounter.mounted == {}, "the slot that was mounted is unmounted too"
        assert store.applied_record() is None and fstab_block(store) == []
        assert _records.version == "1.0.0", "the slot reports the base again"
        assert events(store)[-1] == "dropped"

    def test_nothing_applied_is_nothing_to_check(self, store: ParameterSetStore):
        assert (
            store.check_at_startup(BASE_ID, slot_records=FakeSlotRecords("", None))
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
        assert store.check_at_startup(BASE_ID, slot_records=_records) == _new
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
            store.check_at_startup(BASE_ID, slot_records=self.records_of(_new)) == _new
        )
        assert not store.previous_dir.exists()
        assert mounter.shows(PARAMS.path, store.tree_of(PARAMS))

    def test_a_left_over_discard_is_deleted(self, store: ParameterSetStore):
        _record = apply_set(store, PARAMS)
        (store.discard_dir / "tree").mkdir(parents=True)
        assert (
            store.check_at_startup(BASE_ID, slot_records=self.records_of(_record))
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
            store.check_at_startup(BASE_ID, slot_records=self.records_of(_old)) == _old
        )
        assert mounter.shows(PARAMS.path, store.tree_of(PARAMS))
        assert runner.ran == [PARAMS.apply]

    def test_a_roll_back_that_fails_is_not_recorded_as_recovered(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        _old = apply_set(store, PARAMS)
        stage(store, PARAMS, content="v2")
        store.switch()
        mounter.fail_umount = True
        store.check_at_startup(BASE_ID, slot_records=self.records_of(_old))
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
        assert store.check_at_startup(BASE_ID, slot_records=_records) == _record
        assert PARAMS.path in mounter.mounted
        assert "apply_failed" in events(store)
        assert _records.version == _record.firmware_version

    def test_a_set_of_another_image_is_named_in_the_history(
        self, store: ParameterSetStore
    ):
        _record = apply_set(store, PARAMS)
        store.check_at_startup("another-image", slot_records=self.records_of(_record))
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
        self, store: ParameterSetStore, tmp_path: Path
    ):
        _record = apply_set(store, PARAMS)
        _standby = tmp_path / "standby"

        store.carry_history_to(_standby, reason="full OTA to 2.0.0")

        _carried = _standby / "opt/ota/parameter-set/history.jsonl"
        _events = [json.loads(_l) for _l in _carried.read_text().splitlines()]
        assert [_e["event"] for _e in _events] == ["applied", "left_behind"]
        assert _events[-1]["set"] == _record.set_manifest_digest
        assert not (_standby / "opt/ota/parameter-set/applied").exists()

    def test_a_slot_that_never_had_a_set_carries_nothing(
        self, store: ParameterSetStore, tmp_path: Path
    ):
        store.carry_history_to(tmp_path / "standby", reason="full OTA")
        assert not (tmp_path / "standby").exists()

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
        _mounter.bind_ro(Path("/opt/ota/parameter-set/applied/tree/opt/p"), "/opt/p")
        _mounter.umount("/opt/p")
        assert [_c.args[0] for _c in _run.call_args_list] == [
            ["mount", "--bind", "/opt/ota/parameter-set/applied/tree/opt/p", "/opt/p"],
            ["mount", "-o", "remount,bind,ro,nosuid,nodev", "/opt/p"],
            ["umount", "/opt/p"],
        ]
        assert all(_c.kwargs["set_host_mnt_ns"] for _c in _run.call_args_list)

    def test_a_slot_behind_a_link_is_not_mounted_over(
        self, tmp_path: Path, mocker: MockerFixture
    ):
        _run = mocker.patch.object(parameter_set, "subprocess_run_wrapper")
        (tmp_path / "real").mkdir()
        (tmp_path / "link").symlink_to(tmp_path / "real")
        with pytest.raises(OSError, match="symbolic link"):
            HostParameterSlotMounter().bind_ro(tmp_path / "src", str(tmp_path / "link"))
        _run.assert_not_called()

    def test_apply_runs_in_the_hosts_root_with_a_timeout(self, mocker: MockerFixture):
        _run = mocker.patch.object(parameter_set, "subprocess_run_wrapper")
        mocker.patch.object(
            parameter_set._env,
            "get_dynamic_client_chroot_path",
            return_value="/host_root",
        )
        run_apply("/opt/autoware/bin/apply-parameter-set")
        _run.assert_called_once()
        assert _run.call_args.args[0] == ["/opt/autoware/bin/apply-parameter-set"]
        assert _run.call_args.kwargs["chroot"] == "/host_root"
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
        assert _wrapper.call_args.args[0] == ["umount", "/opt/p"]

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
