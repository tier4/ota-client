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
from ota_image_libs.v1.parameter_set.schema import (
    ParameterSetConfig,
    ParameterSetFileEntry,
)
from ota_image_libs.v1.parameter_set.slots import ParameterSlot, ParameterSlotRef

from otaclient.parameter_set import (
    CONFIG_FNAME,
    FSTAB_BEGIN,
    FSTAB_END,
    TREE_DNAME,
    AppliedRecord,
    ParameterSetFailed,
    ParameterSetStore,
    RunningBase,
)

PARAMS = ParameterSlot(
    name="individual_params",
    path="/opt/autoware/vehicle/vehicle.j6_gen2/individual_params",
    apply="/opt/autoware/bin/apply-parameter-set",
)
LAUNCH = ParameterSlot(
    name="launch",
    path="/opt/autoware/vehicle/vehicle.j6_gen2/launch",
    apply="/opt/autoware/bin/apply-parameter-set",
)
REBOOT = ParameterSlot(name="kernel", path="/etc/kernel-params", apply="reboot")
BASE_DIGEST = Sha256Digest(sha256(b"base manifest").hexdigest())
FSTAB = "UUID=abc\t/\text4\terrors=remount-ro\t0\t1\n"


class FakeMounter:
    def __init__(self) -> None:
        self.mounted: dict[str, Path] = {}
        self.calls: list[tuple] = []
        self.fail_mount = False

    def is_mounted(self, mount_point: str) -> bool:
        return mount_point in self.mounted

    def bind_ro(self, source: Path, mount_point: str) -> None:
        self.calls.append(("mount", str(source), mount_point))
        if self.fail_mount:
            raise OSError("mount failed")
        assert source.is_dir(), f"{source} is not there"
        self.mounted[mount_point] = source

    def umount(self, mount_point: str) -> None:
        self.calls.append(("umount", mount_point))
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
    store: ParameterSetStore, *slots: ParameterSlot, content: str = "v1"
) -> AppliedRecord:
    """A set in staging, as the updater leaves it: the tree and the config."""
    _staging = store.prepare_staging()
    _files = []
    for _slot in slots:
        _f = _staging / TREE_DNAME / _slot.relpath / "default" / "vehicle.param.yaml"
        _f.parent.mkdir(parents=True)
        _f.write_text(f"{_slot.name}: {content}\n")
        _files.append(
            ParameterSetFileEntry(
                path=f"{_slot.relpath}/default/vehicle.param.yaml",
                size=_f.stat().st_size,
                digest=Sha256Digest(sha256(_f.read_bytes()).hexdigest()),
            )
        )
    _config = ParameterSetConfig(
        name="p",
        version=content,
        slots=[ParameterSlotRef(name=_s.name, path=_s.path) for _s in slots],
        diff_ids=[Sha256Digest("1" * 64)] if slots else [],
        files=_files,
    )
    (_staging / CONFIG_FNAME).write_text(_config.export_metafile())
    return AppliedRecord(
        set_manifest_digest=f"sha256:{sha256(content.encode()).hexdigest()}",
        name="p",
        version=content,
        slots=list(slots),
        base_manifest_digest=str(BASE_DIGEST),
        base_image_id="base-image",
        firmware_version=content,
        release_name=f"release-{content}",
        release_id="rid",
        image_id=f"parameter-image-{content}",
        base_version="1.0.0",
        base_version_detail={
            "release_name": "base",
            "release_id": "b",
            "image_id": "base-image",
        },
    )


def fstab_block(store: ParameterSetStore) -> list[str]:
    _lines = store.fstab.read_text().splitlines()
    if FSTAB_BEGIN not in _lines:
        return []
    return _lines[_lines.index(FSTAB_BEGIN) + 1 : _lines.index(FSTAB_END)]


def history(store: ParameterSetStore) -> list[dict]:
    return [json.loads(_l) for _l in store.history_file.read_text().splitlines()]


class TestSwitch:
    def test_a_set_is_bound_over_its_slots_now_and_at_boot(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        _record = stage(store, PARAMS, LAUNCH)
        assert store.switch(new=_record, base_slots=[PARAMS, LAUNCH]) is False

        assert store.applied_record() == _record
        assert not store.staging_dir.exists()
        assert set(mounter.mounted) == {PARAMS.path, LAUNCH.path}
        assert mounter.mounted[PARAMS.path] == (
            store.applied_dir / TREE_DNAME / PARAMS.relpath
        )
        _block = fstab_block(store)
        assert len(_block) == 2
        assert _block[0].split("\t")[1:] == [
            PARAMS.path,
            "none",
            "bind,ro,nofail",
            "0",
            "0",
        ]
        assert store.fstab.read_text().startswith(FSTAB), "the rest of fstab is kept"
        assert runner.ran == [PARAMS.apply], "one apply, run once"
        _event = history(store)[-1]
        assert _event["event"] == "applied"
        assert _event["image_id"] == "parameter-image-v1"
        assert _event["base_image_id"] == "base-image"

    def test_a_new_set_replaces_the_old_one(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        store.switch(new=stage(store, PARAMS, LAUNCH), base_slots=[PARAMS, LAUNCH])
        _new = stage(store, PARAMS, content="v2")
        store.switch(new=_new, base_slots=[PARAMS, LAUNCH])

        assert store.applied_record() == _new
        assert set(mounter.mounted) == {PARAMS.path}, "launch is back to the base's"
        assert len(fstab_block(store)) == 1
        assert FSTAB_BEGIN in store.fstab.read_text().splitlines()
        assert store.fstab.read_text().count(FSTAB_BEGIN) == 1
        assert not (store.root / "previous").exists()

    def test_an_empty_set_gives_every_slot_the_base_defaults(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        store.switch(new=stage(store, PARAMS), base_slots=[PARAMS])
        _empty = stage(store, content="v2")
        store.switch(new=_empty, base_slots=[PARAMS])

        assert mounter.mounted == {}
        assert fstab_block(store) == []
        assert store.fstab.read_text() == FSTAB
        assert runner.ran == [PARAMS.apply, PARAMS.apply], "the slot changed back"
        assert store.applied_record() == _empty, "the release is still recorded"

    def test_a_failed_apply_puts_the_old_set_back(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        _old = stage(store, PARAMS)
        store.switch(new=_old, base_slots=[PARAMS])
        runner.fail_times = 1

        with pytest.raises(ParameterSetFailed):
            store.switch(new=stage(store, PARAMS, content="v2"), base_slots=[PARAMS])

        assert store.applied_record() == _old
        assert (
            mounter.mounted[PARAMS.path]
            == store.applied_dir / TREE_DNAME / PARAMS.relpath
        )
        assert (
            store.applied_dir
            / TREE_DNAME
            / PARAMS.relpath
            / "default"
            / "vehicle.param.yaml"
        ).read_text() == "individual_params: v1\n"
        assert len(fstab_block(store)) == 1
        assert runner.ran[-1] == PARAMS.apply, "the apply runs again for the old set"
        assert history(store)[-1]["event"] == "rolled_back"

    def test_a_first_set_that_fails_leaves_the_base(
        self, store: ParameterSetStore, mounter: FakeMounter
    ):
        mounter.fail_mount = True
        with pytest.raises(ParameterSetFailed):
            store.switch(new=stage(store, PARAMS), base_slots=[PARAMS])
        assert store.applied_record() is None
        assert fstab_block(store) == []
        assert mounter.mounted == {}

    def test_a_slot_whose_apply_is_reboot_asks_for_one(self, store: ParameterSetStore):
        assert store.switch(new=stage(store, REBOOT), base_slots=[REBOOT]) is True

    def test_paths_with_spaces_are_escaped_in_fstab(self, store: ParameterSetStore):
        _spaced = ParameterSlot(name="s", path="/opt/a b", apply="reboot")
        store.switch(new=stage(store, _spaced), base_slots=[_spaced])
        assert "/opt/a\\040b" in fstab_block(store)[0]


class TestAtStartup:
    def base(self) -> RunningBase:
        return RunningBase(manifest_digest=BASE_DIGEST, slots=[PARAMS])

    def test_a_set_that_holds_stays(self, store: ParameterSetStore):
        _record = stage(store, PARAMS)
        store.switch(new=_record, base_slots=[PARAMS])
        _restored = []
        assert (
            store.check_at_startup(self.base(), restore_base_version=_restored.append)
            == _record
        )
        assert _restored == []
        assert history(store)[-1]["event"] == "running"

    def test_a_changed_file_drops_the_set(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        _record = stage(store, PARAMS)
        store.switch(new=_record, base_slots=[PARAMS])
        (
            store.applied_dir
            / TREE_DNAME
            / PARAMS.relpath
            / "default"
            / "vehicle.param.yaml"
        ).write_text("tampered\n")
        _restored = []

        assert (
            store.check_at_startup(self.base(), restore_base_version=_restored.append)
            is None
        )
        assert _restored == [_record], "the slot reports its base again"
        assert mounter.mounted == {}
        assert fstab_block(store) == []
        assert store.applied_record() is None
        assert runner.ran[-1] == PARAMS.apply
        _event = history(store)[-1]
        assert _event["event"] == "dropped" and "differs" in _event["reason"]

    def test_an_extra_file_drops_the_set(self, store: ParameterSetStore):
        store.switch(new=stage(store, PARAMS), base_slots=[PARAMS])
        (store.applied_dir / TREE_DNAME / PARAMS.relpath / "extra").write_text("x")
        assert (
            store.check_at_startup(self.base(), restore_base_version=lambda _r: None)
            is None
        )

    def test_a_set_of_another_base_is_dropped(self, store: ParameterSetStore):
        store.switch(new=stage(store, PARAMS), base_slots=[PARAMS])
        _other = RunningBase(manifest_digest=Sha256Digest("0" * 64), slots=[PARAMS])
        assert (
            store.check_at_startup(_other, restore_base_version=lambda _r: None) is None
        )
        assert (
            store.check_at_startup(None, restore_base_version=lambda _r: None) is None
        )

    def test_a_slot_that_did_not_mount_at_boot_is_mounted_and_applied(
        self, store: ParameterSetStore, mounter: FakeMounter, runner: FakeRunner
    ):
        _record = stage(store, PARAMS)
        store.switch(new=_record, base_slots=[PARAMS])
        mounter.mounted.clear()
        runner.ran.clear()

        assert (
            store.check_at_startup(self.base(), restore_base_version=lambda _r: None)
            == _record
        )
        assert PARAMS.path in mounter.mounted
        assert runner.ran == [PARAMS.apply]
        assert [_e["event"] for _e in history(store)[-2:]] == ["remounted", "running"]

    def test_nothing_applied_is_nothing_to_check(self, store: ParameterSetStore):
        assert (
            store.check_at_startup(self.base(), restore_base_version=lambda _r: None)
            is None
        )
        assert not store.history_file.exists()


class TestHistory:
    def test_a_full_ota_carries_the_history_and_says_the_set_stays_behind(
        self, store: ParameterSetStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _record = stage(store, PARAMS)
        store.switch(new=_record, base_slots=[PARAMS])
        _standby = tmp_path / "standby"

        store.carry_history_to(_standby, reason="full OTA to 2.0.0")

        _carried = _standby / "opt/ota/parameter-set/history.jsonl"
        _events = [json.loads(_l) for _l in _carried.read_text().splitlines()]
        assert [_e["event"] for _e in _events] == ["applied", "left_behind"]
        assert _events[-1]["set"] == _record.set_manifest_digest
        assert not (_standby / "opt/ota/parameter-set/applied").exists()
