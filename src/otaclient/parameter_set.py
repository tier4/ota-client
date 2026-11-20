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

A parameter set names directories of the running base (its slots) and replaces them,
without a standby slot and without a reboot (unless a changed slot's `apply` is
`reboot`). Its files are unpacked into the store in the running slot, and each slot
the set fills is bound read-only over the base's directory: now, by a mount, and at
every boot, by an entry in /etc/fstab ordered before local-fs.target, so that the slot
holds the set before anything that reads it starts. The base's own files are never
written, only hidden; a slot directory the base does not have is made (empty) as the
mount point. A set names no base: it is applied on whatever base the ECU runs, as long
as that base has room for its slots and the applies it does not carry itself (an
`apply` inside the set's slots is the set's own file).

A slot's mount is taken away lazily, so that a program running from the slot's files
keeps them until it exits. The `apply` of a slot runs once the slot holds a set's new
contents; nothing runs when a slot is taken away (an empty set, a set without that
slot, a set dropped): that takes effect when what reads the slot restarts.

The store, under PARAMETER_SET_DPATH::

    applied/        the set this slot runs with
      record.json   what it is, and what it was applied on (AppliedRecord); read by
                    others too, see docs/parameter_set.md
      files.json    the files otaclient unpacked and checked, what is checked again
      manifest.json, config.json   the set's own metadata, as downloaded
      tree/<path>   the slots' files, at their paths relative to /
    previous/       the set before, kept until the new one is recorded as the slot's
    discard/        the set before, being deleted once the new one is recorded
    staging/        a set being prepared
    history.jsonl   the evidence: one JSON object per line, per event
    last-running-boot   what was last recorded as running, so that it is once per boot

A switch is two-phase: the new set is put in place with the old one kept as
`previous/`, and only once the slot records the new release is `previous/` dropped
(`commit`, by one rename to `discard/`). A `previous/` found at startup is a switch
that did not finish: it is completed when the slot already records the applied set's
release, and rolled back otherwise.

What the ECU runs changes by an update only. Unpacked files found other than the
set's at startup are kept as they are and recorded (`tree_mismatch`); the next update
of the set puts its files in place again and applies every slot. A set is dropped
only when it cannot be told what it is (its record is unreadable), when it was
applied on another image than the running one (by the image's version file), or when
a slot cannot be mounted.

A full OTA writes a new slot from an image: its fstab is the image's, so the entries
do not follow and the new base runs with its slots' defaults. Only the history is
carried over, with a line saying the set was left behind.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ota_image_libs.common.oci_spec import Sha256Digest
from ota_image_libs.v1.parameter_set.schema import ParameterSetFileEntry
from ota_image_libs.v1.parameter_set.slots import (
    APPLY_REBOOT,
    MOUNT_POINT_MODE,
    ParameterSlot,
)
from pydantic import BaseModel, ConfigDict, TypeAdapter

from otaclient._types import VersionDetail
from otaclient.configs.cfg import cfg
from otaclient_common import _env
from otaclient_common._io import (
    file_sha256,
    read_str_from_file,
    write_str_to_file_atomic,
)
from otaclient_common.linux import subprocess_run_wrapper

logger = logging.getLogger(__name__)

APPLIED, PREVIOUS, DISCARD, STAGING = "applied", "previous", "discard", "staging"
RECORD_FNAME = "record.json"
RECORD_SCHEMA_VERSION = 1
"""The record's fields that others read (docs/parameter_set.md) keep their meaning
within a schema version."""
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
MOUNT_OPTIONS = "ro,nosuid,nodev"
"""A slot may hold executables (a set may carry binaries and launch files), but
nothing in it runs with privilege it does not have, and it holds no device."""
FSTAB_OPTIONS = f"bind,{MOUNT_OPTIONS},nofail,x-systemd.before=local-fs.target"
"""`nofail` alone also drops the ordering before local-fs.target (systemd.mount(5)):
it is put back, so that what reads a slot starts after the set is bound, while a
slot that cannot be mounted still does not stop the boot."""
STDERR_TAIL = 1024
"""How much of a failed command's error output is kept in its error, from the end."""

_FILES = TypeAdapter(list[ParameterSetFileEntry])
_FSTAB_FIELDS = re.compile(r"[ \t]+")


class ParameterSetFailed(Exception):
    """A set could not be put in place. `restored` tells whether the slots are back
    where they were (the set before, or the base's defaults), or could not be put
    back either."""

    def __init__(self, msg: str, *, restored: bool) -> None:
        super().__init__(msg)
        self.restored = restored


class CommandFailed(Exception):
    """A command otaclient runs for a parameter set failed: what it was, how it ended
    and the end of what it wrote to stderr."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _boot_id() -> str:
    return read_str_from_file(BOOT_ID_FPATH, _default="")


def _stderr_tail(stderr: bytes | str | None) -> str:
    if not stderr:
        return ""
    if isinstance(stderr, str):
        stderr = stderr.encode()
    return stderr[-STDERR_TAIL:].decode(errors="replace").strip()


def run_command(cmd: list[str], *, what: str, **kwargs: Any) -> None:
    """Run `cmd` (subprocess_run_wrapper's keyword arguments), raising CommandFailed
    with the end of its stderr, which is logged too: an `apply` or a mount that fails
    says why, in the log and in the failure the ECU reports."""
    try:
        subprocess_run_wrapper(cmd, check=True, check_output=False, **kwargs)
    except subprocess.CalledProcessError as e:
        _err = _stderr_tail(e.stderr)
        logger.error(f"{what} failed with exit code {e.returncode}: {_err}")
        raise CommandFailed(
            f"{what} failed with exit code {e.returncode}"
            + (f": {_err}" if _err else "")
        ) from e
    except subprocess.TimeoutExpired as e:
        _err = _stderr_tail(e.stderr)
        logger.error(f"{what} timed out after {e.timeout}s: {_err}")
        raise CommandFailed(
            f"{what} timed out after {e.timeout}s" + (f": {_err}" if _err else "")
        ) from e


#
# ------ what is applied ------ #
#


class RecordedSlot(BaseModel):
    """A slot as the record keeps it: otaclient's own model, so that a record this
    otaclient wrote stays readable whatever the image library's slot model becomes.
    The slot was checked as a ParameterSlot when its set was applied."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    path: str
    apply: str

    @property
    def relpath(self) -> str:
        """The path as a tree relative to `/` names it."""
        return self.path[1:]

    @classmethod
    def of(cls, slot: ParameterSlot) -> RecordedSlot:
        return cls(name=slot.name, path=slot.path, apply=slot.apply)


class AppliedRecord(BaseModel):
    """A set as this slot applied it, what it was applied on, and the release that
    named it.

    `set_*` is the set itself. `firmware_version`, `release_*` and `image_id` are the
    release the request named, `image_id` the parameter image's. `base_image_id` is
    the running image's (its version file) when the set was applied, empty when it
    has none: with `image_id` it says that this release runs on this base.
    `base_version` and `base_version_detail` are what the slot recorded before any
    set, so that a dropped set reports the base again.

    `schema_version`, `image_id`, `set_name`, `set_version`, `base_image_id` and
    `applied_at` are read by others (docs/parameter_set.md).
    """

    schema_version: int = RECORD_SCHEMA_VERSION
    set_manifest_digest: str
    set_name: str
    set_version: str
    slots: list[RecordedSlot]
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

    def is_recorded_by(self, records: CurrentSlotRecords) -> bool:
        """Whether the current slot records this set's release as its own."""
        _version, _detail = records.load_recorded_version()
        return _version == self.firmware_version and (
            (_detail.image_id if _detail else "") == self.image_id
        )


class ParameterSlotMounter(Protocol):
    def is_mounted(self, mount_point: str) -> bool: ...
    def make_mount_point(self, mount_point: str) -> bool: ...
    def bind_ro(self, source: Path, mount_point: str) -> None: ...
    def umount(self, mount_point: str) -> None: ...


class ApplyRunner(Protocol):
    def __call__(self, command: str) -> None: ...


class CurrentSlotRecords(Protocol):
    """The version records of the A/B slot otaclient runs from (the boot
    controller's), not a parameter slot's."""

    def load_recorded_version(self) -> tuple[str, VersionDetail | None]: ...
    def restore_recorded_version(
        self, *, version: str, version_detail: VersionDetail | None
    ) -> None: ...


class HostParameterSlotMounter:
    """Mounts in the host's mount namespace, also from an app image: what the
    device's processes see is the host's view, and so are the paths checked.

    The store and /etc/fstab are used at their paths as they are: an app image binds
    the host's /opt and /etc at the same paths (samples/otaclient.service_app).
    """

    def __init__(self, host_root: str | None = None) -> None:
        self._host_root = (
            host_root or _env.get_dynamic_client_chroot_path() or cfg.CANONICAL_ROOT
        )

    @staticmethod
    def _host_mnt_ns() -> bool:
        return _env.is_dynamic_client_running()

    def is_mounted(self, mount_point: str) -> bool:
        try:
            subprocess_run_wrapper(
                ["findmnt", "--noheadings", "--mountpoint", mount_point],
                check=True,
                check_output=False,
                set_host_mnt_ns=self._host_mnt_ns(),
            )
            return True
        except Exception:
            return False

    def _on_host(self, mount_point: str) -> str:
        """The slot's path as declared, on the host, not a link someone put there."""
        _root = os.path.realpath(self._host_root)
        _on_host = os.path.join(_root, mount_point.lstrip("/"))
        if os.path.realpath(_on_host) != _on_host:
            raise OSError(f"{mount_point} is behind a symbolic link")
        return _on_host

    def make_mount_point(self, mount_point: str) -> bool:
        """Make the slot's directory when the host does not have it (a slot the base
        has no directory for), with its missing parents, as root's with mode 0755
        whatever the umask; True when it was made. Left in place when the slot is
        taken away: an empty directory.
        Raises OSError for a path behind a link, or with a file in the way."""
        _on_host = self._on_host(mount_point)
        if os.path.isdir(_on_host):
            return False
        # each step is opened by its parent's fd and not followed when it is a link,
        #   and only what was made here is chmod'ed, by its own fd: a directory swapped
        #   for a link meanwhile fails (ELOOP), nothing outside the path changes
        _fd = os.open(
            os.path.realpath(self._host_root),
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            for _component in mount_point.strip("/").split("/"):
                try:
                    os.mkdir(_component, MOUNT_POINT_MODE, dir_fd=_fd)
                    _made = True
                except FileExistsError:
                    _made = False
                _next = os.open(
                    _component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=_fd,
                )
                os.close(_fd)
                _fd = _next
                if _made:
                    os.fchmod(_fd, MOUNT_POINT_MODE)
        finally:
            os.close(_fd)
        return True

    def bind_ro(self, source: Path, mount_point: str) -> None:
        self._on_host(mount_point)
        run_command(
            ["mount", "--bind", str(source), mount_point],
            what=f"binding {mount_point}",
            set_host_mnt_ns=self._host_mnt_ns(),
        )
        try:
            run_command(
                ["mount", "-o", f"remount,bind,{MOUNT_OPTIONS}", mount_point],
                what=f"making {mount_point} read-only",
                set_host_mnt_ns=self._host_mnt_ns(),
            )
        except Exception:
            # not left bound writable: the bind goes before the failure is raised
            with contextlib.suppress(Exception):
                self.umount(mount_point)
            raise

    def umount(self, mount_point: str) -> None:
        # lazy: a program that runs from the slot's files (what an overlay holds)
        #   keeps them until it exits, rather than the slot staying as it was
        run_command(
            ["umount", "--lazy", mount_point],
            what=f"unmounting {mount_point}",
            set_host_mnt_ns=self._host_mnt_ns(),
        )


def run_apply(command: str) -> None:
    """Run a slot's `apply` in the host's root and mount namespace: what it runs and
    reads is what the device's processes see, the slots' mounts included (an `apply`
    the set carries is a file on one of them), also from an app image."""
    logger.info(f"running {command} ...")
    run_command(
        [command],
        what=f"apply {command}",
        # entering it resets the root to the host's: no chroot as well, as the
        #   mounter does
        set_host_mnt_ns=_env.is_dynamic_client_running(),
        timeout=cfg.PARAMETER_SET_APPLY_TIMEOUT,
    )


def _fstab_escape(path: str) -> str:
    return path.replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011")


def _fstab_unescape(field: str) -> str:
    return field.replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")


def _file_key(entry: ParameterSetFileEntry) -> tuple[str, int, str]:
    """What tells two files of a set apart: path, size and digest."""
    return (entry.path, entry.size, str(entry.digest))


def _slot_files(
    files: Iterable[ParameterSetFileEntry], slot: RecordedSlot
) -> frozenset[tuple[str, int, str]]:
    return frozenset(
        _file_key(_f) for _f in files if _f.path.startswith(slot.relpath + "/")
    )


def _slot_key(slot: RecordedSlot) -> tuple[str, str]:
    """A slot is the same in two sets when its name and its path are."""
    return (slot.name, slot.path)


class ParameterSetStore:
    """The store in the running slot, the fstab entries, the mounts and the history."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        fstab: Path | None = None,
        mounter: ParameterSlotMounter | None = None,
        runner: ApplyRunner | None = None,
    ) -> None:
        self.root = root or Path(cfg.PARAMETER_SET_DPATH)
        self.fstab = fstab or Path(cfg.FSTAB_FPATH)
        self._mounter = mounter or HostParameterSlotMounter()
        self._runner = runner or run_apply

    @property
    def applied_dir(self) -> Path:
        return self.root / APPLIED

    @property
    def previous_dir(self) -> Path:
        return self.root / PREVIOUS

    @property
    def discard_dir(self) -> Path:
        return self.root / DISCARD

    @property
    def staging_dir(self) -> Path:
        return self.root / STAGING

    @property
    def history_file(self) -> Path:
        return self.root / HISTORY_FNAME

    def is_mounted(self, mount_point: str) -> bool:
        return self._mounter.is_mounted(mount_point)

    def tree_of(self, slot: RecordedSlot, set_dir: Path | None = None) -> Path:
        """The unpacked directory of `slot` in the set in `set_dir` (applied/)."""
        return (set_dir or self.applied_dir) / TREE_DNAME / slot.relpath

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

    def _fstab_lines(self) -> tuple[list[str], list[str]]:
        """The lines of /etc/fstab outside otaclient's block, and those inside it.
        Lines are split as libmount splits them: at `\\n` only."""
        try:
            _text = self.fstab.read_text()
        except FileNotFoundError:
            return [], []
        _lines = _text.split("\n")
        if _lines and _lines[-1] == "":
            _lines.pop()
        _kept: list[str] = []
        _block: list[str] = []
        _inside = False
        for _l in _lines:
            if _l.strip(" \t") == FSTAB_BEGIN:
                _inside = True
            elif _l.strip(" \t") == FSTAB_END:
                _inside = False
            elif _inside:
                _block.append(_l)
            else:
                _kept.append(_l)
        return _kept, _block

    def _fstab_mount_points(self) -> list[str]:
        """The mount points of otaclient's block of /etc/fstab: what it mounts when
        its record cannot tell."""
        _points: list[str] = []
        for _l in self._fstab_lines()[1]:
            _fields = _FSTAB_FIELDS.split(_l.strip(" \t"))
            if len(_fields) >= 2:
                _points.append(_fstab_unescape(_fields[1]))
        return _points

    def _write_fstab(self, record: AppliedRecord | None) -> None:
        """Rewrite otaclient's block of /etc/fstab: one bind entry per slot the
        applied set fills, or no block at all."""
        _kept, _ = self._fstab_lines()
        if record is not None and record.slots:
            _kept.append(FSTAB_BEGIN)
            for _slot in record.slots:
                _kept.append(
                    f"{_fstab_escape(str(self.tree_of(_slot)))}\t"
                    f"{_fstab_escape(_slot.path)}\tnone\t{FSTAB_OPTIONS}\t0\t0"
                )
            _kept.append(FSTAB_END)
        write_str_to_file_atomic(self.fstab, "\n".join(_kept) + "\n")

    # ------ mounts and applies ------ #

    def _unmount(self, mount_points: Iterable[str]) -> None:
        for _point in dict.fromkeys(mount_points):
            while self._mounter.is_mounted(_point):
                # lazily, with whatever is mounted below it: said, as it goes
                logger.info(f"taking the mount at {_point} away")
                self._mounter.umount(_point)

    def _set_aside_applied(self) -> None:
        """Rename applied/ to discard/ (one rename, so that the set is not the
        applied one any more whatever fails after); discard/ goes later, or at the
        next start."""
        shutil.rmtree(self.discard_dir, ignore_errors=True)
        if self.applied_dir.exists():
            os.replace(self.applied_dir, self.discard_dir)

    @staticmethod
    def _paths(*records: AppliedRecord | None) -> list[str]:
        """The slots' paths of `records`: what otaclient mounted for them."""
        return [_s.path for _r in records if _r is not None for _s in _r.slots]

    def _mount(self, record: AppliedRecord | None) -> None:
        if record is None:
            return
        for _slot in record.slots:
            if not self._mounter.is_mounted(_slot.path):
                if self._mounter.make_mount_point(_slot.path):
                    logger.info(
                        f"made {_slot.path}, the mount point of slot {_slot.name}"
                    )
                    self.record_event(
                        "mount_point_made", slot=_slot.name, path=_slot.path
                    )
                self._mounter.bind_ro(self.tree_of(_slot), _slot.path)

    def _run_applies(self, slots: Iterable[RecordedSlot]) -> bool:
        """Run each distinct `apply` of `slots` once; True when one of them asks for a
        reboot instead."""
        _reboot = False
        for _command in dict.fromkeys(_s.apply for _s in slots):
            if _command == APPLY_REBOOT:
                _reboot = True
                continue
            self._runner(_command)
        return _reboot

    def _put_in_place(
        self, record: AppliedRecord | None, changed: Iterable[RecordedSlot]
    ) -> bool:
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
    ) -> list[RecordedSlot]:
        """The slots `new` fills whose contents change from `old`, with the `apply`
        that makes each change take effect (`new`'s, the file at its path once `new`
        is in place): filled by `new` only, or by both with other files (by path,
        size and digest) or another `apply`. A slot `new` takes away (filled by `old`
        only) is not among them: nothing runs for it, it takes effect when what reads
        it restarts. A slot is the same in both by its name and path: one that moved
        is taken away at the old path and filled at the new one."""
        _old_slots = {_slot_key(_s): _s for _s in (old.slots if old else [])}
        _new_slots = {_slot_key(_s): _s for _s in new.slots}
        _changed: list[RecordedSlot] = []
        for _key in dict.fromkeys([*_old_slots, *_new_slots]):
            _o, _n = _old_slots.get(_key), _new_slots.get(_key)
            if _n is None:
                continue
            if (
                _o is None
                or _o.apply != _n.apply
                or _slot_files(old_files, _o) != _slot_files(new_files, _n)
            ):
                _changed.append(_n)
        return _changed

    def _slots_to_apply(
        self,
        old: AppliedRecord | None,
        old_dir: Path,
        new: AppliedRecord,
        new_files: list[ParameterSetFileEntry],
    ) -> list[RecordedSlot]:
        """The slots `new` fills whose contents change from the set in `old_dir`: by
        their file lists, unless the files in `old_dir` are not its set's own, when
        every slot `new` fills changes."""
        if old is not None and (_why := self.check_tree(old, old_dir)) is not None:
            logger.warning(
                f"the files of {old.describe()} are not the set's ({_why}): "
                "every slot is applied"
            )
            return list(new.slots)
        return self.changed_slots(old, self._read_files(old_dir), new, new_files)

    def switch(self) -> bool:
        """Put the staged set in place of the applied one, keeping that one as
        `previous/` until `commit`, and run the `apply` of every slot the new set
        fills whose contents change; nothing runs for a slot it takes away.

        On failure the applied set is put back, its `apply`s run again, and
        ParameterSetFailed raised, saying whether that worked. Returns True when a
        changed slot's `apply` is `reboot`.
        """
        _new = self._read_record(self.staging_dir)
        assert _new is not None, "stage() first"
        _old = self.applied_record()
        _changed = self._slots_to_apply(
            _old, self.applied_dir, _new, self._read_files(self.staging_dir)
        )
        shutil.rmtree(self.previous_dir, ignore_errors=True)
        _renamed = False
        try:
            # the new set's slots too: a switch that failed before may have left one
            #   (anything else mounted there was refused before the update began)
            self._unmount([*self._paths(_old, _new), *self._fstab_mount_points()])
            if self.applied_dir.exists():
                os.replace(self.applied_dir, self.previous_dir)
            _renamed = True
            os.replace(self.staging_dir, self.applied_dir)
            return self._put_in_place(_new, _changed)
        except Exception as e:
            logger.error(f"failed to put the parameter set in place: {e}; rolling back")
            if _renamed:
                _restored = self.roll_back(cause=e)
            else:
                # nothing was moved: the applied set is still the old one, only its
                #   mounts may be gone
                _restored = self._remount(_old, cause=e)
            raise ParameterSetFailed(
                f"the set was not put in place: {e}", restored=_restored
            ) from e

    def _remount(self, record: AppliedRecord | None, *, cause: Exception) -> bool:
        try:
            self._mount(record)
            self.record_event("rolled_back", cause=str(cause), remounted=True)
            return True
        except Exception as e:
            logger.error(f"failed to mount the applied set again: {e}")
            self.record_event("roll_back_failed", cause=str(cause), error=str(e))
            return False

    def commit(self) -> None:
        """The slot records the new set's release: the one before goes for good.

        One rename marks it gone before it is deleted: a crash while it is deleted
        leaves `discard/`, not a `previous/` that would be taken for a switch that did
        not finish. Never raises: the update has succeeded by now.
        """
        try:
            if self.previous_dir.exists():
                shutil.rmtree(self.discard_dir, ignore_errors=True)
                os.replace(self.previous_dir, self.discard_dir)
        except Exception as e:
            logger.warning(f"failed to set the set before aside: {e!r}")
        shutil.rmtree(self.discard_dir, ignore_errors=True)
        shutil.rmtree(self.previous_dir, ignore_errors=True)
        if (_record := self.applied_record()) is not None:
            self.record_event("applied", **_record.evidence())

    def roll_back(self, *, cause: Exception) -> bool:
        """Put `previous/` (or no set, when there was none) back in place of the
        applied set, and run the `apply` of the slots the set put back fills that
        change back; nothing runs for a slot it does not fill. Never raises: returns
        whether the slots are back, a failure is recorded."""
        try:
            _failed = self.applied_record()
            _back = self._read_record(self.previous_dir)
            if _failed is not None and _back is not None:
                _changed = self._slots_to_apply(
                    _failed,
                    self.applied_dir,
                    _back,
                    self._read_files(self.previous_dir),
                )
            else:
                # one side is not there (crashed between the two renames of a switch,
                #   or no set before): every slot the set put back fills changes
                _changed = list(_back.slots) if _back else []
            _points = [*self._paths(_failed, _back), *self._fstab_mount_points()]
            # first, by renames, the failed set leaves applied/ (for discard/) and
            #   the set put back (or none) takes its place, and what mounts at boot
            #   is the set put back's: whatever fails below, a later boot and
            #   otaclient's next start lay the set put back, not the failed one
            self._set_aside_applied()
            if self.previous_dir.exists():
                os.replace(self.previous_dir, self.applied_dir)
            self._write_fstab(_back)
            self._unmount(_points)
            shutil.rmtree(self.discard_dir, ignore_errors=True)
            self._put_in_place(_back, _changed)
            self.record_event(
                "rolled_back",
                cause=str(cause),
                set=_back.set_manifest_digest if _back else None,
            )
            return True
        except Exception as e:
            logger.error(f"failed to roll the parameter set back: {e}")
            self.record_event("roll_back_failed", cause=str(cause), error=str(e))
            return False

    def _complete_switch(self) -> None:
        """A switch whose release the slot already records, crashed before its commit
        finished: its mounts are put back from applied/ (a slot bound before the
        rename sees previous/), then it is committed. No `apply` runs: the switch ran
        them, or the boot since restarted what reads the slots."""
        _applied = self.applied_record()
        self._unmount([*self._paths(_applied), *self._fstab_mount_points()])
        self._put_in_place(_applied, ())
        self.commit()

    # ------ the applied set, verified ------ #

    def check_tree(
        self, record: AppliedRecord, set_dir: Path | None = None
    ) -> str | None:
        """Why the unpacked files of the set in `set_dir` (the applied one by default)
        are not what otaclient unpacked and checked, or None: every file of its list,
        with its size and digest, and nothing else."""
        set_dir = set_dir or self.applied_dir
        try:
            _expected = {_f.path: _f for _f in self._read_files(set_dir)}
        except Exception as e:
            return f"the list of the set's files is unreadable: {e!r}"
        _tree = set_dir / TREE_DNAME
        _found: set[str] = set()
        for _slot in record.slots:
            _slot_dir = self.tree_of(_slot, set_dir)
            if not _slot_dir.is_dir() or _slot_dir.is_symlink():
                return f"slot {_slot.name!r} is missing"
            for _dirpath, _dirnames, _filenames in os.walk(_slot_dir):
                for _name in _dirnames:
                    # os.walk lists a link to a directory here and does not follow it
                    if (Path(_dirpath) / _name).is_symlink():
                        _rel = (Path(_dirpath) / _name).relative_to(_tree).as_posix()
                        return f"{_rel} is not a directory"
                for _name in _filenames:
                    _p = Path(_dirpath) / _name
                    _rel = _p.relative_to(_tree).as_posix()
                    _st = _p.lstat()
                    if not stat.S_ISREG(_st.st_mode):
                        return f"{_rel} is not a regular file"
                    _want = _expected.get(_rel)
                    if _want is None:
                        return f"{_rel} is not in the set"
                    if _want.size != _st.st_size or _want.digest != Sha256Digest(
                        file_sha256(_p)
                    ):
                        return f"{_rel} differs from the set"
                    _found.add(_rel)
        if _missing := sorted(set(_expected) - _found):
            return f"{len(_missing)} files of the set are missing, {_missing[0]} first"
        return None

    def check_at_startup(
        self, running_image_id: str, *, slot_records: CurrentSlotRecords
    ) -> AppliedRecord | None:
        """At otaclient's start: the applied set, kept as the last update left it.

        `running_image_id` is the running image's ID from its version file, "" when
        it has none (or none otaclient reads), as `base_image_id` was recorded.

        The fstab entries mounted the set at boot, before anything read it; this is
        where it is checked, and where that is recorded. A switch that did not finish
        (`previous/` still there) is completed when the slot records the applied
        set's release, and rolled back otherwise. Files other than the set's are kept
        and recorded: what the ECU runs changes by an update only. The set is dropped
        (the slots get the base's defaults, the slot reports the base) only when its
        record is unreadable, when it was applied on another image than the running
        one, or when a slot cannot be mounted; an `apply` that fails after a slot was
        mounted again is recorded, the set kept.
        """
        shutil.rmtree(self.discard_dir, ignore_errors=True)
        if self.previous_dir.exists():
            self._finish_switch(slot_records)

        try:
            _record = self._read_record(self.applied_dir)
        except Exception as e:
            self._drop(None, f"the set's record is unreadable: {e!r}")
            return None
        if _record is None:
            self._clear_stale_fstab()
            return None
        logger.info(
            f"parameter set {_record.describe()} is applied on image "
            f"{_record.base_image_id or '(no version file)'}"
        )

        if running_image_id != _record.base_image_id:
            # not this slot's set (copied in with the slot): its records are not of it
            self._drop(
                None,
                f"the set was applied on image {_record.base_image_id!r}, the slot "
                f"runs image {running_image_id!r}",
                mount_points=self._paths(_record),
                evidence=_record.evidence(),
            )
            return None
        if (_mismatch := self.check_tree(_record)) is not None:
            logger.error(
                f"the files of {_record.describe()} are not the set's ({_mismatch}): "
                "kept as they are until the next update of the set"
            )

        if sorted(self._fstab_mount_points()) != sorted(self._paths(_record)):
            # a switch or a drop cut short between its steps: what mounts at boot is
            #   the applied set's again
            logger.warning(
                f"otaclient's fstab block is not {_record.describe()}'s: writing it"
            )
            self._write_fstab(_record)
            self.record_event("fstab_restored", set=_record.set_manifest_digest)
        _unmounted = [
            _s for _s in _record.slots if not self._mounter.is_mounted(_s.path)
        ]
        if _unmounted and not self._mount_again(
            _record, _unmounted, slot_records=slot_records
        ):
            return None

        self._record_running(_record, _mismatch)
        return _record

    def _clear_stale_fstab(self) -> None:
        """No set is applied, but otaclient's fstab block mounts some (a roll back or
        a drop cut short between its steps): it goes, and so does what it mounted."""
        _stale = self._fstab_mount_points()
        if not _stale:
            return
        logger.warning(
            f"otaclient's fstab block mounts {_stale} with no set applied: clearing it"
        )
        try:
            self._write_fstab(None)
            self._unmount(_stale)
            self.record_event("fstab_cleared", mount_points=_stale)
        except Exception as e:
            logger.error(f"failed to clear otaclient's fstab block: {e}")
            self.record_event("fstab_clear_failed", error=str(e))

    def _finish_switch(self, slot_records: CurrentSlotRecords) -> None:
        try:
            _applied = self._read_record(self.applied_dir)
        except Exception:
            _applied = None
        if _applied is not None and _applied.is_recorded_by(slot_records):
            logger.warning(
                f"the switch to {_applied.describe()} was recorded but not committed: "
                "completing it"
            )
            self._complete_switch()
            self.record_event("recovered", completed=True)
            return
        logger.warning("a parameter set switch did not finish: rolling it back")
        if self.roll_back(cause=RuntimeError("an unfinished switch")):
            self.record_event("recovered", completed=False)

    def _mount_again(
        self,
        record: AppliedRecord,
        unmounted: list[RecordedSlot],
        *,
        slot_records: CurrentSlotRecords,
    ) -> bool:
        """Slots of the applied set that are not mounted: fstab's entries are
        `nofail`, so they ran with the base's defaults until now. They are mounted and
        their `apply` runs again; a slot that cannot be mounted drops the set (False),
        an `apply` that fails is recorded and the set kept (True)."""
        logger.warning(
            f"slots {[_s.name for _s in unmounted]} are not mounted: "
            "mounting them and running their apply"
        )
        try:
            # the slots not mounted are `unmounted`: _mount lays those only, making
            #   a mount point that has gone
            self._mount(record)
        except Exception as e:
            self._drop(
                record,
                f"the set could not be mounted: {e}",
                slot_records=slot_records,
            )
            return False
        self.record_event(
            "remounted",
            set=record.set_manifest_digest,
            slots=[_s.name for _s in unmounted],
        )
        try:
            self._reboot_required(self._run_applies(unmounted), "remounted")
        except Exception as e:
            logger.error(f"the apply of the slots mounted again failed: {e}")
            self.record_event(
                "apply_failed", set=record.set_manifest_digest, error=str(e)
            )
        return True

    def _record_running(self, record: AppliedRecord, mismatch: str | None) -> None:
        """`running` once per boot, `tree_mismatch` once per boot and finding: an
        otaclient restarting in a loop adds no lines."""
        _boot = _boot_id()
        _seen = read_str_from_file(self.root / LAST_RUNNING_FNAME, _default="")
        # one line: what is compared with what the file gives back
        _key = _boot if mismatch is None else " ".join([_boot, *mismatch.split()])
        if _seen == _key:
            return
        if _seen.split(" ", 1)[0] != _boot:
            self.record_event("running", boot_id=_boot, **record.evidence())
        if mismatch is not None:
            self.record_event(
                "tree_mismatch",
                reason=mismatch,
                boot_id=_boot,
                set=record.set_manifest_digest,
            )
        write_str_to_file_atomic(self.root / LAST_RUNNING_FNAME, _key)

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
        why: str,
        *,
        slot_records: CurrentSlotRecords | None = None,
        mount_points: list[str] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        """Drop the applied set: the slot reports the base again (when its records
        are still this set's release), the mounts and the fstab entries go, then the
        unpacked files. No `apply` runs: the slots go back to the base's when what
        reads them restarts.

        Without a `record` to go by (unreadable, or not this slot's), the slot's
        records are left as they are; what is unmounted is `mount_points` and what
        otaclient's fstab block mounts, and what the history says of the set is
        `evidence`."""
        _what = record.describe() if record else "the applied set"
        logger.error(f"{_what} does not hold ({why}): dropping it")
        _evidence = record.evidence() if record else (evidence or {})
        try:
            if (
                record is not None
                and slot_records is not None
                and record.is_recorded_by(slot_records)
            ):
                slot_records.restore_recorded_version(
                    version=record.base_version,
                    version_detail=record.base_version_detail,
                )
            _points = [
                *self._paths(record),
                *(mount_points or []),
                *self._fstab_mount_points(),
            ]
            # first, by a rename, the set leaves applied/ and what mounts at boot
            #   lays nothing: whatever fails below, neither a later boot nor
            #   otaclient's next start lays it again
            self._set_aside_applied()
            self._write_fstab(None)
            self._unmount(_points)
            shutil.rmtree(self.discard_dir, ignore_errors=True)
        except Exception as e:
            logger.error(f"failed to drop {_what}: {e}")
            self.record_event("drop_failed", reason=why, error=str(e), **_evidence)
            return
        self.record_event("dropped", reason=why, **_evidence)

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
