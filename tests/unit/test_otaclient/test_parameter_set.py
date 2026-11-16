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
    FSTAB_OPTIONS,
    HISTORY_MAX_SIZE,
    TREE_DNAME,
    AppliedRecord,
    HostSlotMounter,
    ParameterSetFailed,
    ParameterSetStore,
    RunningBase,
    run_apply,
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
BASE_DIGEST = Sha256Digest(sha256(b"base manifest").hexdigest())
FSTAB = "UUID=abc\t/\text4\terrors=remount-ro\t0\t1\n"
BASE_DETAIL = VersionDetail(release_name="base", release_id="b", image_id="base-image")


class FakeMounter:
    def __init__(self) -> None:
        self.mounted: dict[str, Path] = {}
        self.fail_mount = False
        self.fail_umount = False

    def is_mounted(self, mount_point: str) -> bool:
        return mount_point in self.mounted

    def bind_ro(self, source: Path, mount_point: str) -> None:
        if self.fail_mount:
            raise OSError("mount failed")
        assert source.is_dir(), f"{source} is not there"
        assert mount_point not in self.mounted, "bound twice"
        self.mounted[mount_point] = source

    def umount(self, mount_point: str) -> None:
        if self.fail_umount:
            raise OSError("target is busy")
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
        base_manifest_digest=str(BASE_DIGEST),
        base_image_id="base-image",
        firmware_version=content,
        release_name=f"release-{content}",
        release_id="rid",
        image_id=f"parameter-image-{content}",
        base_version="1.0.0",
        base_version_detail=BASE_DETAIL,
    )
    store.stage(_record, _files)
    return _record


def apply_set(store: ParameterSetStore, *slots, base_slots, **kw) -> AppliedRecord:
    _record = stage(store, *slots, **kw)
    store.switch(base_slots=base_slots)
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
        assert store.switch(base_slots=[PARAMS, LAUNCH]) is False
        store.commit()

        assert store.applied_record() == _record
        assert not store.staging_dir.exists() and not store.previous_dir.exists()
        assert set(mounter.mounted) == {PARAMS.path, LAUNCH.path}
        assert mounter.mounted[PARAMS.path] == (
            store.applied_dir / TREE_DNAME / PARAMS.relpath
        )
        _block = fstab_block(store)
        assert len(_block) == 2
        assert _block[0].split("\t")[1:] == [
            PARAMS.path,
            "none",
            FSTAB_OPTIONS,
            "0",
            "0",
        ]
        assert "nosuid" in FSTAB_OPTIONS and "noexec" in FSTAB_OPTIONS
        assert store.fstab.read_text().startswith(FSTAB), "the rest of fstab is kept"
        assert runner.ran == [PARAMS.apply, LAUNCH.apply]
        _event = history(store)[-1]
        assert _event["event"] == "applied"
        assert _event["image_id"] == "parameter-image-v1"
        assert _event["base_image_id"] == "base-image"

    def test_only_the_slots_whose_files_change_are_applied(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        apply_set(store, PARAMS, LAUNCH, base_slots=[PARAMS, LAUNCH])
        runner.ran.clear()
        _new = apply_set(
            store,
            PARAMS,
            LAUNCH,
            base_slots=[PARAMS, LAUNCH],
            content="v2",
            contents={LAUNCH.name: "v1"},
        )

        assert store.applied_record() == _new
        assert runner.ran == [PARAMS.apply], "launch holds the same files"
        assert set(mounter.mounted) == {PARAMS.path, LAUNCH.path}

    def test_the_same_set_again_applies_nothing(
        self, store: ParameterSetStore, runner: FakeRunner
    ):
        apply_set(store, REBOOT, PARAMS, base_slots=[REBOOT, PARAMS])
        runner.ran.clear()
        _record = stage(store, REBOOT, PARAMS)
        assert store.switch(base_slots=[REBOOT, PARAMS]) is False, "no reboot"
        assert runner.ran == []

    def test_a_new_set_without_a_slot_gives_it_back_the_base_defaults(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        apply_set(store, PARAMS, LAUNCH, base_slots=[PARAMS, LAUNCH])
        runner.ran.clear()
        apply_set(store, PARAMS, base_slots=[PARAMS, LAUNCH])

        assert set(mounter.mounted) == {PARAMS.path}
        assert len(fstab_block(store)) == 1
        assert store.fstab.read_text().count(FSTAB_BEGIN) == 1
        assert runner.ran == [LAUNCH.apply]

    def test_an_empty_set_gives_every_slot_the_base_defaults(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        apply_set(store, PARAMS, base_slots=[PARAMS])
        _empty = apply_set(store, base_slots=[PARAMS], content="v2")

        assert mounter.mounted == {}
        assert fstab_block(store) == []
        assert store.fstab.read_text() == FSTAB
        assert runner.ran == [PARAMS.apply, PARAMS.apply], "the slot changed back"
        assert store.applied_record() == _empty, "the release is still recorded"

    def test_a_failed_apply_puts_the_old_set_back(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        _old = apply_set(store, PARAMS, base_slots=[PARAMS])
        runner.fail_times = 1

        stage(store, PARAMS, content="v2")
        with pytest.raises(ParameterSetFailed) as _e:
            store.switch(base_slots=[PARAMS])

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
        assert (
            mounter.mounted[PARAMS.path]
            == store.applied_dir / TREE_DNAME / PARAMS.relpath
        )
        assert len(fstab_block(store)) == 1
        assert runner.ran[-1] == PARAMS.apply, "the apply runs again for the old set"
        assert events(store)[-1] == "rolled_back"

    def test_a_first_set_that_fails_leaves_the_base(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        mounter.fail_mount = True
        stage(store, PARAMS)
        with pytest.raises(ParameterSetFailed) as _e:
            store.switch(base_slots=[PARAMS])
        assert _e.value.restored
        assert store.applied_record() is None
        assert fstab_block(store) == []
        assert mounter.mounted == {}

    def test_a_busy_slot_leaves_the_applied_set_where_it_was(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        """Nothing was moved yet: the old set stays, mounted."""
        _old = apply_set(store, PARAMS, base_slots=[PARAMS])
        mounter.fail_umount = True
        stage(store, PARAMS, content="v2")
        with pytest.raises(ParameterSetFailed) as _e:
            store.switch(base_slots=[PARAMS])

        assert _e.value.restored
        assert store.applied_record() == _old
        assert PARAMS.path in mounter.mounted
        assert len(fstab_block(store)) == 1

    def test_a_set_that_cannot_be_put_back_says_so(
        self, store: ParameterSetStore, runner: FakeRunner
    ):
        """The new set's apply fails, then the old set's: the slot runs neither as
        it should."""
        apply_set(store, PARAMS, base_slots=[PARAMS])
        runner.fail_times = 2
        stage(store, PARAMS, content="v2")
        with pytest.raises(ParameterSetFailed) as _e:
            store.switch(base_slots=[PARAMS])
        assert not _e.value.restored
        assert events(store)[-1] == "roll_back_failed"

    def test_the_old_set_comes_back_when_the_release_cannot_be_recorded(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        """What the updater does when the slot's records fail after the switch."""
        _old = apply_set(store, PARAMS, base_slots=[PARAMS])
        stage(store, PARAMS, content="v2")
        store.switch(base_slots=[PARAMS])
        assert store.previous_dir.exists(), "kept until the release is recorded"

        assert store.roll_back(base_slots=[PARAMS], cause=OSError("disk full"))

        assert store.applied_record() == _old
        assert runner.ran[-1] == PARAMS.apply
        assert "applied" not in events(store)[1:]

    def test_a_slot_whose_apply_is_reboot_asks_for_one(self, store: ParameterSetStore):
        stage(store, REBOOT)
        assert store.switch(base_slots=[REBOOT]) is True

    def test_paths_with_spaces_are_escaped_in_fstab(self, store: ParameterSetStore):
        _spaced = ParameterSlot(name="s", path="/opt/a b", apply="reboot")
        stage(store, _spaced)
        store.switch(base_slots=[_spaced])
        assert "/opt/a\\040b" in fstab_block(store)[0]


class TestAtStartup:
    def base(self, *slots: ParameterSlot) -> RunningBase:
        return RunningBase(manifest_digest=BASE_DIGEST, slots=list(slots or [PARAMS]))

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
        _record = apply_set(store, PARAMS, base_slots=[PARAMS])
        _records = self.records_of(_record)
        assert store.check_at_startup(self.base(), slot_records=_records) == _record
        assert store.check_at_startup(self.base(), slot_records=_records) == _record
        assert events(store).count("running") == 1, "a restart adds no line"
        boot("boot-2")
        store.check_at_startup(self.base(), slot_records=_records)
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
        _record = apply_set(store, PARAMS, base_slots=[PARAMS])
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

        assert store.check_at_startup(self.base(), slot_records=_records) == _record
        assert _records.version == _record.firmware_version
        assert _records.detail and _records.detail.image_id == _record.image_id
        assert PARAMS.path in mounter.mounted and len(fstab_block(store)) == 1
        assert _f.read_text() == "tampered\n", "not put back either"
        assert runner.ran == []
        _events = history(store)
        assert [_e["event"] for _e in _events[-2:]] == ["running", "tree_mismatch"]
        assert "differs" in _events[-1]["reason"]

        store.check_at_startup(self.base(), slot_records=_records)
        assert events(store).count("tree_mismatch") == 1, "a restart adds no line"
        boot("boot-2")
        store.check_at_startup(self.base(), slot_records=_records)
        assert events(store)[-2:] == ["running", "tree_mismatch"]
        assert events(store).count("tree_mismatch") == 2

    def test_a_changed_set_that_did_not_mount_at_boot_is_mounted_as_it_is(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        _record = apply_set(store, PARAMS, base_slots=[PARAMS])
        (store.applied_dir / TREE_DNAME / PARAMS.relpath / "extra").write_text("x")
        mounter.mounted.clear()
        runner.ran.clear()
        assert (
            store.check_at_startup(self.base(), slot_records=self.records_of(_record))
            == _record
        )
        assert PARAMS.path in mounter.mounted
        assert runner.ran == [PARAMS.apply]

    def test_the_next_update_of_a_changed_set_puts_its_files_back_and_applies(
        self, store: ParameterSetStore, runner: FakeRunner
    ):
        """The same set again: by the file lists nothing changes, but the files in
        place are not those, so every slot is put in place and applied."""
        _record = apply_set(store, PARAMS, base_slots=[PARAMS])
        _f = (
            store.applied_dir
            / TREE_DNAME
            / PARAMS.relpath
            / "default"
            / "vehicle.param.yaml"
        )
        _f.write_text("tampered\n")
        runner.ran.clear()

        apply_set(store, PARAMS, base_slots=[PARAMS])

        assert runner.ran == [PARAMS.apply]
        assert _f.read_text() == "individual_params: v1\n"
        assert store.check_tree(_record) is None

    def test_the_same_set_again_is_not_applied_when_its_files_hold(
        self, store: ParameterSetStore, runner: FakeRunner
    ):
        apply_set(store, PARAMS, base_slots=[PARAMS])
        runner.ran.clear()
        apply_set(store, PARAMS, base_slots=[PARAMS])
        assert runner.ran == []

    def test_a_slot_that_records_another_release_keeps_it_on_a_drop(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        apply_set(store, PARAMS, base_slots=[PARAMS])
        del mounter.mounted[PARAMS.path]
        mounter.fail_mount = True
        _records = FakeSlotRecords("9.9.9", VersionDetail(image_id="another"))
        assert store.check_at_startup(self.base(), slot_records=_records) is None
        assert _records.version == "9.9.9"

    def test_a_set_of_another_base_is_dropped_without_touching_the_records(
        self, store: ParameterSetStore, runner: FakeRunner
    ):
        """A set copied in with the slot: the slot's records are the new base's."""
        _record = apply_set(store, PARAMS, base_slots=[PARAMS])
        runner.ran.clear()
        _records = self.records_of(_record)
        _other = RunningBase(manifest_digest=Sha256Digest("0" * 64), slots=[PARAMS])
        assert store.check_at_startup(_other, slot_records=_records) is None
        assert _records.version == _record.firmware_version
        assert store.applied_record() is None and fstab_block(store) == []
        assert runner.ran == []

    def test_an_unreadable_record_drops_the_set(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        apply_set(store, PARAMS, base_slots=[PARAMS])
        (store.applied_dir / "record.json").write_text("")
        _records = FakeSlotRecords("x", None)
        assert store.check_at_startup(self.base(), slot_records=_records) is None
        assert not store.applied_dir.exists()
        assert mounter.mounted == {} and fstab_block(store) == []
        assert "unreadable" in history(store)[-1]["reason"]

    def test_an_unfinished_switch_is_rolled_back(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        """Power lost after the switch, before the release was recorded."""
        _old = apply_set(store, PARAMS, base_slots=[PARAMS])
        stage(store, PARAMS, content="v2")
        store.switch(base_slots=[PARAMS])

        assert (
            store.check_at_startup(self.base(), slot_records=self.records_of(_old))
            == _old
        )
        assert not store.previous_dir.exists()
        assert events(store)[-3:] == ["rolled_back", "recovered", "running"]

    def test_a_drop_whose_apply_fails_is_recorded_as_such(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        _record = apply_set(store, PARAMS, base_slots=[PARAMS])
        del mounter.mounted[PARAMS.path]
        mounter.fail_mount = True
        runner.fail_times = 1
        _records = self.records_of(_record)
        store.check_at_startup(self.base(), slot_records=_records)
        assert events(store)[-1] == "drop_failed"
        assert _records.version == "1.0.0", "the base's records come back first"

    def test_a_slot_that_did_not_mount_at_boot_is_mounted_and_applied(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        _record = apply_set(store, PARAMS, REBOOT, base_slots=[PARAMS, REBOOT])
        mounter.mounted.clear()
        runner.ran.clear()

        assert (
            store.check_at_startup(
                self.base(PARAMS, REBOOT), slot_records=self.records_of(_record)
            )
            == _record
        )
        assert PARAMS.path in mounter.mounted
        assert runner.ran == [PARAMS.apply]
        assert events(store)[-3:] == ["remounted", "reboot_required", "running"]

    def test_a_slot_that_cannot_be_mounted_drops_the_set(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        _record = apply_set(store, PARAMS, LAUNCH, base_slots=[PARAMS, LAUNCH])
        del mounter.mounted[LAUNCH.path]
        mounter.fail_mount = True
        assert (
            store.check_at_startup(
                self.base(PARAMS, LAUNCH), slot_records=self.records_of(_record)
            )
            is None
        )
        assert mounter.mounted == {}, "the slot that was mounted is unmounted too"

    def test_nothing_applied_is_nothing_to_check(self, store: ParameterSetStore):
        assert (
            store.check_at_startup(self.base(), slot_records=FakeSlotRecords("", None))
            is None
        )
        assert not store.history_file.exists()


class TestHistory:
    def test_a_full_ota_carries_the_history_and_says_the_set_stays_behind(
        self, store: ParameterSetStore, tmp_path: Path
    ):
        _record = apply_set(store, PARAMS, base_slots=[PARAMS])
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
        _mounter = HostSlotMounter()
        _mounter.bind_ro(Path("/opt/ota/parameter-set/applied/tree/opt/p"), "/opt/p")
        _mounter.umount("/opt/p")
        assert [_c.args[0] for _c in _run.call_args_list] == [
            ["mount", "--bind", "/opt/ota/parameter-set/applied/tree/opt/p", "/opt/p"],
            ["mount", "-o", "remount,bind,ro,nosuid,nodev,noexec", "/opt/p"],
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
            HostSlotMounter().bind_ro(tmp_path / "src", str(tmp_path / "link"))
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
