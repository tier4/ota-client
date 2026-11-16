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
"""Parameter sets laid over the running base (spec: ota-image-libs parameter_set.md).

A parameter set replaces the directories its base declares as parameter slots,
without a standby slot and without a reboot. Its files are unpacked into the store
in the running slot, and each slot the set fills is bound read-only over the base's
directory: now, by a mount, and at every boot, by an entry in /etc/fstab, so that
the slot holds the set before anything that reads it starts. The base's own files are
never written, only hidden.

The store, under PARAMETER_SET_DPATH::

    applied/        the set this slot runs with
      record.json   what it is, and what it was applied on (AppliedRecord)
      files.json    the files otaclient unpacked and checked, what is checked again
      manifest.json, config.json   the set's own metadata, as downloaded
      tree/<path>   the slots' files, at their paths relative to /
    previous/       the set before, kept until the new one is recorded as the slot's
    staging/        a set being prepared
    history.jsonl   the evidence: one JSON object per line, per event

A switch is two-phase: the new set is put in place with the old one kept as
`previous/`, and only once the slot records the new release is `previous/` dropped
(`commit`). A `previous/` found at startup is a switch that did not finish, and is
rolled back.

A full OTA writes a new slot from an image: its fstab is the image's, so the entries
do not follow and the new base runs with its slots' defaults. Only the history is
carried over, with a line saying the set was left behind.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
from collections.abc import Iterable
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from ota_image_libs.common.oci_spec import Sha256Digest
from ota_image_libs.v1.image_config.schema import ImageConfig
from ota_image_libs.v1.parameter_set.schema import ParameterSetFileEntry
from ota_image_libs.v1.parameter_set.slots import APPLY_REBOOT, ParameterSlot
from pydantic import BaseModel, TypeAdapter

from ota_metadata.v1 import IMAGE_CONFIG_SAVE_FNAME, IMAGE_MANIFEST_SAVE_FNAME
from otaclient._types import VersionDetail
from otaclient.configs.cfg import cfg
from otaclient_common import _env
from otaclient_common._io import read_str_from_file, write_str_to_file_atomic
from otaclient_common.linux import subprocess_run_wrapper

logger = logging.getLogger(__name__)

APPLIED, PREVIOUS, STAGING = "applied", "previous", "staging"
RECORD_FNAME = "record.json"
FILES_FNAME = "files.json"
MANIFEST_FNAME = "manifest.json"
CONFIG_FNAME = "config.json"
TREE_DNAME = "tree"
HISTORY_FNAME = "history.jsonl"
HISTORY_ROTATED_FNAME = "history.jsonl.1"
HISTORY_MAX_SIZE = 1024**2
"""The history rotates past this: the file and the one before it are kept."""
LAST_RUNNING_FNAME = "last-running-boot"
BOOT_ID_FPATH = "/proc/sys/kernel/random/boot_id"

FSTAB_BEGIN = "# BEGIN otaclient parameter set (managed by otaclient, do not edit)"
FSTAB_END = "# END otaclient parameter set"
MOUNT_OPTIONS = "ro,nosuid,nodev,noexec"
"""A slot holds data: nothing in it is executed with privilege, or at all."""
FSTAB_OPTIONS = f"bind,{MOUNT_OPTIONS},nofail"

_FILES = TypeAdapter(list[ParameterSetFileEntry])


class ParameterSetFailed(Exception):
    """A set could not be put in place; the slot is back where it was."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _boot_id() -> str:
    return read_str_from_file(BOOT_ID_FPATH, _default="")


#
# ------ the running base ------ #
#


class RunningBase(BaseModel):
    """The base payload this slot was installed from, as a full OTA kept it."""

    manifest_digest: Sha256Digest
    slots: list[ParameterSlot]


def load_running_base(image_meta_dir: Path) -> RunningBase | None:
    """The running base's manifest digest and slots, from what a full OTA saved in
    image-meta (the session's own copies, under the same names); None when it saved
    nothing (an otaclient before parameter sets, or a legacy OTA image)."""
    try:
        _manifest = (image_meta_dir / IMAGE_MANIFEST_SAVE_FNAME).read_bytes()
        _config = ImageConfig.parse_metafile(
            (image_meta_dir / IMAGE_CONFIG_SAVE_FNAME).read_text()
        )
    except FileNotFoundError:
        return None
    return RunningBase(
        manifest_digest=Sha256Digest(sha256(_manifest).hexdigest()),
        slots=_config.parameter_slots or [],
    )


#
# ------ what is applied ------ #
#


class AppliedRecord(BaseModel):
    """A set as this slot applied it, what it was applied on, and the release that
    named it.

    `set_*` is the set itself. `firmware_version`, `release_*` and `image_id` are the
    release the request named, `image_id` the parameter image's. `base_image_id` is
    the running image's (its version file) when the set was applied: with `image_id`
    it says that this release runs on this base. `base_version` and
    `base_version_detail` are what the slot recorded before any set, so that a
    dropped set reports the base again.
    """

    set_manifest_digest: str
    set_name: str
    set_version: str
    slots: list[ParameterSlot]
    base_manifest_digest: str
    base_image_id: str = ""
    firmware_version: str = ""
    release_name: str = ""
    release_id: str = ""
    image_id: str = ""
    base_version: str = ""
    base_version_detail: VersionDetail | None = None
    applied_at: str = ""
    session_id: str = ""

    def evidence(self) -> dict[str, Any]:
        """What the history says of it."""
        return self.model_dump(
            include={
                "set_manifest_digest",
                "set_name",
                "set_version",
                "base_manifest_digest",
                "base_image_id",
                "firmware_version",
                "release_name",
                "release_id",
                "image_id",
                "session_id",
            }
        ) | {"slots": [_s.name for _s in self.slots]}

    def describe(self) -> str:
        return f"{self.set_name} {self.set_version} ({self.set_manifest_digest})"


class SlotMounter(Protocol):
    def is_mounted(self, mount_point: str) -> bool: ...
    def bind_ro(self, source: Path, mount_point: str) -> None: ...
    def umount(self, mount_point: str) -> None: ...


class ApplyRunner(Protocol):
    def __call__(self, command: str) -> None: ...


class SlotRecords(Protocol):
    """The current slot's version records (the boot controller's)."""

    def load_recorded_version(self) -> tuple[str, VersionDetail | None]: ...
    def restore_recorded_version(
        self, *, version: str, version_detail: VersionDetail | None
    ) -> None: ...


class HostSlotMounter:
    """Mounts in the host's mount namespace, also from an app image: what the
    device's processes see is the host's view."""

    def _run(self, cmd: list[str]) -> None:
        subprocess_run_wrapper(
            cmd,
            check=True,
            check_output=False,
            set_host_mnt_ns=_env.is_dynamic_client_running(),
        )

    def is_mounted(self, mount_point: str) -> bool:
        try:
            self._run(["findmnt", "--noheadings", "--mountpoint", mount_point])
            return True
        except Exception:
            return False

    def bind_ro(self, source: Path, mount_point: str) -> None:
        # the slot's path as declared, not a link someone put there in its stead
        if os.path.realpath(mount_point) != mount_point:
            raise OSError(f"{mount_point} is behind a symbolic link")
        self._run(["mount", "--bind", str(source), mount_point])
        self._run(["mount", "-o", f"remount,bind,{MOUNT_OPTIONS}", mount_point])

    def umount(self, mount_point: str) -> None:
        self._run(["umount", mount_point])


def run_apply(command: str) -> None:
    """Run a slot's `apply`, an executable of the base, in the host's root."""
    logger.info(f"running {command} ...")
    subprocess_run_wrapper(
        [command],
        check=True,
        check_output=False,
        chroot=_env.get_dynamic_client_chroot_path(),
        timeout=cfg.PARAMETER_SET_APPLY_TIMEOUT,
    )


def _fstab_escape(path: str) -> str:
    return path.replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011")


def _slot_files(
    files: Iterable[ParameterSetFileEntry], slot: ParameterSlot
) -> frozenset[tuple[str, int, str]]:
    return frozenset(
        (_f.path, _f.size, str(_f.digest))
        for _f in files
        if _f.path.startswith(slot.relpath + "/")
    )


class ParameterSetStore:
    """The store in the running slot, the fstab entries, the mounts and the history."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        fstab: Path | None = None,
        mounter: SlotMounter | None = None,
        runner: ApplyRunner | None = None,
    ) -> None:
        self.root = root or Path(cfg.PARAMETER_SET_DPATH)
        self.fstab = fstab or Path(cfg.FSTAB_FPATH)
        self._mounter = mounter or HostSlotMounter()
        self._runner = runner or run_apply

    @property
    def applied_dir(self) -> Path:
        return self.root / APPLIED

    @property
    def previous_dir(self) -> Path:
        return self.root / PREVIOUS

    @property
    def staging_dir(self) -> Path:
        return self.root / STAGING

    @property
    def history_file(self) -> Path:
        return self.root / HISTORY_FNAME

    # ------ records ------ #

    def applied_record(self) -> AppliedRecord | None:
        """The applied set's record; None when there is none, or when it cannot be
        read (`check_at_startup` drops such a set)."""
        try:
            return self._read_record(self.applied_dir)
        except Exception as e:
            logger.warning(f"the applied set's record is unreadable: {e!r}")
            return None

    @staticmethod
    def _read_record(set_dir: Path) -> AppliedRecord | None:
        try:
            _raw = (set_dir / RECORD_FNAME).read_text()
        except FileNotFoundError:
            return None
        return AppliedRecord.model_validate_json(_raw)

    @staticmethod
    def _read_files(set_dir: Path) -> list[ParameterSetFileEntry]:
        try:
            return _FILES.validate_json((set_dir / FILES_FNAME).read_text())
        except FileNotFoundError:
            return []

    def record_event(self, event: str, **fields: Any) -> None:
        """Append one line of evidence. Never raises: the history is a record of
        what happened, not a condition for it."""
        _line = json.dumps({"time": _now(), "event": event, **fields}, sort_keys=True)
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if (
                self.history_file.exists()
                and self.history_file.stat().st_size > HISTORY_MAX_SIZE
            ):
                os.replace(self.history_file, self.root / HISTORY_ROTATED_FNAME)
            with open(self.history_file, "a") as _f:
                _f.write(_line + "\n")
                _f.flush()
                os.fsync(_f.fileno())
        except Exception as e:
            logger.warning(f"failed to record {event} in the history: {e!r}")

    def prepare_staging(self) -> Path:
        shutil.rmtree(self.staging_dir, ignore_errors=True)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.staging_dir.mkdir(mode=0o700)
        return self.staging_dir

    def discard_staging(self) -> None:
        shutil.rmtree(self.staging_dir, ignore_errors=True)

    def stage(self, record: AppliedRecord, files: list[ParameterSetFileEntry]) -> None:
        """Write the staged set's record and the files that were unpacked for it,
        and flush the staging onto the disk: what is switched to is on the disk."""
        write_str_to_file_atomic(
            self.staging_dir / FILES_FNAME, _FILES.dump_json(files).decode()
        )
        write_str_to_file_atomic(
            self.staging_dir / RECORD_FNAME, record.model_dump_json()
        )
        os.sync()

    # ------ fstab ------ #

    def _write_fstab(self, record: AppliedRecord | None) -> None:
        """Rewrite otaclient's block of /etc/fstab: one bind entry per slot the
        applied set fills, or no block at all."""
        _lines = self.fstab.read_text().splitlines() if self.fstab.exists() else []
        _kept: list[str] = []
        _inside = False
        for _l in _lines:
            if _l.strip() == FSTAB_BEGIN:
                _inside = True
            elif _l.strip() == FSTAB_END:
                _inside = False
            elif not _inside:
                _kept.append(_l)
        if record is not None and record.slots:
            _kept.append(FSTAB_BEGIN)
            for _slot in record.slots:
                _src = self.applied_dir / TREE_DNAME / _slot.relpath
                _kept.append(
                    f"{_fstab_escape(str(_src))}\t{_fstab_escape(_slot.path)}\t"
                    f"none\t{FSTAB_OPTIONS}\t0\t0"
                )
            _kept.append(FSTAB_END)
        write_str_to_file_atomic(self.fstab, "\n".join(_kept) + "\n")

    # ------ mounts and applies ------ #

    def _unmount(self, slots: Iterable[ParameterSlot]) -> None:
        for _slot in slots:
            while self._mounter.is_mounted(_slot.path):
                self._mounter.umount(_slot.path)

    def _mount(self, record: AppliedRecord | None) -> None:
        if record is None:
            return
        for _slot in record.slots:
            if not self._mounter.is_mounted(_slot.path):
                self._mounter.bind_ro(
                    self.applied_dir / TREE_DNAME / _slot.relpath, _slot.path
                )

    def _run_applies(self, slots: Iterable[ParameterSlot]) -> bool:
        """Run each distinct `apply` of `slots` once; True when one of them asks for a
        reboot instead."""
        _reboot = False
        for _command in dict.fromkeys(_s.apply for _s in slots):
            if _command == APPLY_REBOOT:
                _reboot = True
                continue
            self._runner(_command)
        return _reboot

    def _put_in_place(self, record: AppliedRecord | None, changed) -> bool:
        self._write_fstab(record)
        self._mount(record)
        os.sync()
        return self._run_applies(changed)

    @staticmethod
    def changed_slots(
        old: AppliedRecord | None,
        old_files: list[ParameterSetFileEntry],
        new: AppliedRecord,
        new_files: list[ParameterSetFileEntry],
    ) -> list[ParameterSlot]:
        """The slots whose contents change: filled by one set and not the other, or
        filled by both with other files (by path, size and digest)."""
        _old_slots = {_s.name: _s for _s in (old.slots if old else [])}
        _new_slots = {_s.name: _s for _s in new.slots}
        _changed: list[ParameterSlot] = []
        for _name in dict.fromkeys([*_old_slots, *_new_slots]):
            _o, _n = _old_slots.get(_name), _new_slots.get(_name)
            if (
                _o is None
                or _n is None
                or _slot_files(old_files, _o) != _slot_files(new_files, _n)
            ):
                _changed.append(_n or _o)  # type: ignore[arg-type]
        return _changed

    def switch(self, *, base_slots: list[ParameterSlot]) -> bool:
        """Put the staged set in place of the applied one, keeping that one as
        `previous/` until `commit`, and run the `apply` of every slot whose contents
        change.

        On failure the applied set is put back, its `apply`s run again, and
        ParameterSetFailed raised. Returns True when a changed slot's `apply` is
        `reboot`.
        """
        _new = self._read_record(self.staging_dir)
        assert _new is not None, "stage() first"
        _old = self.applied_record()
        _changed = self.changed_slots(
            _old,
            self._read_files(self.applied_dir),
            _new,
            self._read_files(self.staging_dir),
        )
        shutil.rmtree(self.previous_dir, ignore_errors=True)
        _renamed = False
        try:
            self._unmount(base_slots)
            if self.applied_dir.exists():
                os.replace(self.applied_dir, self.previous_dir)
            _renamed = True
            os.replace(self.staging_dir, self.applied_dir)
            return self._put_in_place(_new, _changed)
        except Exception as e:
            logger.error(
                f"failed to put the parameter set in place: {e!r}; rolling back"
            )
            if _renamed:
                self.roll_back(base_slots=base_slots, cause=e)
            else:
                # nothing was moved: the applied set is still the old one, only its
                #   mounts may be gone
                self._remount(_old, cause=e)
            raise ParameterSetFailed(f"the set was not put in place: {e!r}") from e

    def _remount(self, record: AppliedRecord | None, *, cause: Exception) -> None:
        try:
            self._mount(record)
            self.record_event("rolled_back", cause=repr(cause), remounted=True)
        except Exception as e:
            logger.error(f"failed to mount the applied set again: {e!r}")
            self.record_event("roll_back_failed", cause=repr(cause), error=repr(e))

    def commit(self) -> None:
        """The slot records the new set's release: the one before is gone for good."""
        shutil.rmtree(self.previous_dir, ignore_errors=True)
        if (_record := self.applied_record()) is not None:
            self.record_event("applied", **_record.evidence())

    def roll_back(self, *, base_slots: list[ParameterSlot], cause: Exception) -> None:
        """Put `previous/` (or no set, when there was none) back in place of the
        applied set, and run the `apply` of the slots that change back. Never raises;
        a failure is recorded."""
        try:
            _new = self.applied_record()
            _old = self._read_record(self.previous_dir)
            _changed = (
                self.changed_slots(
                    _old,
                    self._read_files(self.previous_dir),
                    _new,
                    self._read_files(self.applied_dir),
                )
                if _new is not None
                else list(_old.slots if _old else [])
            )
            self._unmount(base_slots)
            shutil.rmtree(self.applied_dir, ignore_errors=True)
            if self.previous_dir.exists():
                os.replace(self.previous_dir, self.applied_dir)
            self._put_in_place(_old, _changed)
            self.record_event(
                "rolled_back",
                cause=repr(cause),
                set=_old.set_manifest_digest if _old else None,
            )
        except Exception as e:
            logger.error(f"failed to roll the parameter set back: {e!r}")
            self.record_event("roll_back_failed", cause=repr(cause), error=repr(e))

    # ------ the applied set, verified ------ #

    def check_tree(self, record: AppliedRecord) -> str | None:
        """Why the unpacked files are not what otaclient unpacked and checked, or
        None: every file of its list, with its size and digest, and nothing else."""
        try:
            _expected = {_f.path: _f for _f in self._read_files(self.applied_dir)}
        except Exception as e:
            return f"the list of the set's files is unreadable: {e!r}"
        _tree = self.applied_dir / TREE_DNAME
        _found: set[str] = set()
        for _slot in record.slots:
            _slot_dir = _tree / _slot.relpath
            if not _slot_dir.is_dir() or _slot_dir.is_symlink():
                return f"slot {_slot.name!r} is missing"
            for _dirpath, _dirnames, _filenames in os.walk(_slot_dir):
                for _name in _filenames:
                    _p = Path(_dirpath) / _name
                    _rel = _p.relative_to(_tree).as_posix()
                    _st = _p.lstat()
                    if not stat.S_ISREG(_st.st_mode):
                        return f"{_rel} is not a regular file"
                    _want = _expected.get(_rel)
                    if _want is None:
                        return f"{_rel} is not in the set"
                    if _want.size != _st.st_size or _want.digest != _digest_of(_p):
                        return f"{_rel} differs from the set"
                    _found.add(_rel)
        if _missing := sorted(set(_expected) - _found):
            return f"{len(_missing)} files of the set are missing, {_missing[0]} first"
        return None

    def check_at_startup(
        self, base: RunningBase | None, *, slot_records: SlotRecords
    ) -> AppliedRecord | None:
        """At otaclient's start: the applied set, if it still holds; else it is
        dropped, the slots get the base's defaults and the slot reports the base.

        The fstab entries mounted the set at boot, before anything read it; this is
        where it is checked, and where that is recorded. A switch that did not finish
        (`previous/` still there) is rolled back first.
        """
        _base_slots = base.slots if base else []
        if self.previous_dir.exists():
            logger.warning("a parameter set switch did not finish: rolling it back")
            self.roll_back(
                base_slots=_base_slots, cause=RuntimeError("an unfinished switch")
            )
            self.record_event("recovered")

        try:
            _record = self._read_record(self.applied_dir)
        except Exception as e:
            self._drop(None, _base_slots, f"the set's record is unreadable: {e!r}")
            return None
        if _record is None:
            return None
        logger.info(
            f"parameter set {_record.describe()} is applied on base "
            f"{_record.base_manifest_digest}"
        )

        if base is None or str(base.manifest_digest) != _record.base_manifest_digest:
            # not this slot's set (copied in with the slot): its records are not of it
            self._drop(None, _base_slots, "the set was not applied on the running base")
            return None
        if (_why := self.check_tree(_record)) is not None:
            self._drop(_record, _base_slots, _why, slot_records=slot_records)
            return None

        _unmounted = [
            _s for _s in _record.slots if not self._mounter.is_mounted(_s.path)
        ]
        if _unmounted:
            # fstab's entries are `nofail`: a slot that did not mount at boot ran with
            # the base's defaults until now, so its `apply` runs again
            logger.warning(
                f"slots {[_s.name for _s in _unmounted]} did not mount at boot: "
                "mounting them and running their apply"
            )
            try:
                for _slot in _unmounted:
                    self._mounter.bind_ro(
                        self.applied_dir / TREE_DNAME / _slot.relpath, _slot.path
                    )
                _reboot = self._run_applies(_unmounted)
            except Exception as e:
                self._drop(
                    _record,
                    _base_slots,
                    f"the set could not be mounted: {e!r}",
                    slot_records=slot_records,
                )
                return None
            self.record_event(
                "remounted",
                set=_record.set_manifest_digest,
                slots=[_s.name for _s in _unmounted],
            )
            self._reboot_required(_reboot, "remounted")

        if read_str_from_file(self.root / LAST_RUNNING_FNAME, _default="") != (
            _boot := _boot_id()
        ):
            # once per boot: an otaclient restarting in a loop adds no lines
            self.record_event("running", boot_id=_boot, **_record.evidence())
            write_str_to_file_atomic(self.root / LAST_RUNNING_FNAME, _boot)
        return _record

    def _reboot_required(self, required: bool, after: str) -> None:
        if required:
            # not rebooted from here: a boot that reboots again could loop
            logger.warning(
                f"a slot whose apply is reboot changed ({after}): it takes effect at "
                "the next reboot"
            )
            self.record_event("reboot_required", after=after)

    def _drop(
        self,
        record: AppliedRecord | None,
        base_slots: list[ParameterSlot],
        why: str,
        *,
        slot_records: SlotRecords | None = None,
    ) -> None:
        """Drop the applied set: the slot reports the base again (when its records
        are still this set's release), the mounts and the fstab entries go, then the
        unpacked files, then the slots that changed back are applied."""
        _what = record.describe() if record else "the applied set"
        logger.error(f"{_what} does not hold ({why}): dropping it")
        _evidence = record.evidence() if record else {}
        try:
            if record is not None and slot_records is not None:
                _version, _detail = slot_records.load_recorded_version()
                if _version == record.firmware_version and (
                    (_detail.image_id if _detail else "") == record.image_id
                ):
                    slot_records.restore_recorded_version(
                        version=record.base_version,
                        version_detail=record.base_version_detail,
                    )
            self._unmount(base_slots if record is None else record.slots)
            self._write_fstab(None)
            shutil.rmtree(self.applied_dir, ignore_errors=True)
            _reboot = self._run_applies(record.slots) if record is not None else False
        except Exception as e:
            logger.error(f"failed to drop {_what}: {e!r}")
            self.record_event("drop_failed", reason=why, error=repr(e), **_evidence)
            return
        self.record_event("dropped", reason=why, **_evidence)
        self._reboot_required(_reboot, "dropped")

    def carry_history_to(self, standby_root: Path, *, reason: str) -> None:
        """Copy the history into the slot a full OTA wrote, saying that the applied
        set stays behind. Never raises."""
        try:
            _record = self.applied_record()
            if _record is None and not self.history_file.exists():
                return  # no set was ever applied here: nothing to carry
            _dst = standby_root / Path(cfg.PARAMETER_SET_DPATH).relative_to("/")
            _dst.mkdir(mode=0o700, parents=True, exist_ok=True)
            for _fname in (HISTORY_ROTATED_FNAME, HISTORY_FNAME):
                if (self.root / _fname).exists():
                    shutil.copyfile(self.root / _fname, _dst / _fname)
            ParameterSetStore(root=_dst, fstab=self.fstab).record_event(
                "left_behind",
                reason=reason,
                set=_record.set_manifest_digest if _record else None,
                set_version=_record.set_version if _record else None,
            )
        except Exception as e:
            logger.warning(f"failed to carry the parameter set history: {e!r}")


def _digest_of(path: Path) -> Sha256Digest:
    _h = sha256()
    with open(path, "rb") as _f:
        while _chunk := _f.read(1024**2):
            _h.update(_chunk)
    return Sha256Digest(_h.hexdigest())
