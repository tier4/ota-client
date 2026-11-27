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
"""Overlays laid over the running base (spec: ota-image-libs overlay.md).

An overlay names directories of the running base (its slots) and replaces them,
without a standby slot and without a reboot (unless a changed slot's `apply` is
`reboot`). Its files are unpacked into the store, and each slot the overlay fills is
bound read-only over the base's directory: now, by a mount, and at every boot, by
`otaclient mount-overlay` (`mount_overlay_command`), run by a unit ordered before
local-fs.target, so that the slot holds the overlay before anything that reads it
starts. The base's own files are never written, only hidden; a slot directory the base
does not have is made (empty) as the mount point. An overlay names no base: it is
applied on whatever base the ECU runs, as long as that base has room for its slots and
the applies it does not carry itself (an `apply` inside the overlay's slots is the
overlay's own file).

The store is on /opt/data, the one place every layout can write and keep (a
partition-based layout's root is read-only), and the same mechanism serves every
layout: what differs is only what a read-only root refuses (a slot directory the
image does not have cannot be made there) and what the boot controller does (its
records, and how the same slot is rebooted).

A slot's mount is taken away lazily, so that a program running from the slot's files
keeps them until it exits. The `apply` of a slot runs once the slot holds an overlay's
new contents; nothing runs when a slot is taken away (an empty overlay, an overlay
without that slot, an overlay dropped): that takes effect when what reads the slot
restarts.

The store, under OVERLAY_DPATH::

    applied/        the overlay this slot runs with
      record.json   what it is, and what it was applied on (AppliedRecord); read by
                    others too, see docs/overlay.md
      files.json    the files otaclient unpacked and checked, what is checked again
      manifest.json, config.json   the overlay's own metadata, as downloaded
      tree/<path>   the slots' files, at their paths relative to /
    previous/       the overlay before, kept until the new one is recorded as the slot's
    discard/        the overlay before, being deleted once the new one is recorded
    staging/        an overlay being prepared
    history.jsonl   the evidence: one JSON object per line, per event
    last-running-boot   what was last recorded as running, so that it is once per boot

A switch is two-phase: the new overlay is put in place with the old one kept as
`previous/`, and only once the slot records the new release is `previous/` dropped
(`commit`, by one rename to `discard/`). A `previous/` found at startup is a switch
that did not finish: it is completed when the slot already records the applied overlay's
release, and rolled back otherwise. Every step renames before it mounts: `applied/` is
always what is to be mounted, which is all the boot reads; a `previous/` there, the
boot mounts nothing and leaves the switch to otaclient's start.

What the ECU runs changes by an update only. Unpacked files found other than the
overlay's at startup are kept as they are and recorded (`tree_mismatch`); the next
update of the overlay puts its files in place again and applies every slot. An overlay
is dropped only when it cannot be told what it is (its record is unreadable), when it is
not the running base's (`whose_overlay`: applied on another image, by the image's
version file, or another slot of the same image), or when a slot cannot be mounted. One
that is not the running base's, or that cannot be told, is never laid, and is dropped
only once the running base is committed: a trial boot of a new base over a store the
slots share may still fall back to the base the overlay was applied on, which lays it
again. Nor is it the overlay before when a new overlay is applied: one is refused until
the running base is committed, and then the other base's overlay is replaced.

Nothing is laid from, stored into, recorded in or deleted from a store whose way
from its mount point is not root's alone (`why_not_private`): another user who can
rename a directory on it could plant an overlay whose `apply` runs as root.

A full OTA writes a new slot from an image, and the new base runs with its slots'
defaults. A store on the slot's own root filesystem is not carried over (not even by
the persist files): only the history is, with a line saying the overlay was left behind.
A store on a filesystem of its own (a partition shared by both slots) stays where it
is, with that line and a mark that a full update was staged, and the new slot's
otaclient drops the overlay once the new base is committed: it was applied on another
image, or on another slot of the same image (the slot's records tell). Such a store
needs the base's image ID to tell bases apart: a new overlay is refused on a base
without one.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import stat
import subprocess
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ota_image_libs.common.oci_spec import Sha256Digest
from ota_image_libs.v1.overlay.schema import OverlayFileEntry
from ota_image_libs.v1.overlay.slots import (
    APPLY_REBOOT,
    MOUNT_POINT_MODE,
    OverlaySlot,
)
from pydantic import BaseModel, ConfigDict, TypeAdapter

from otaclient._types import VersionDetail
from otaclient.configs.cfg import cfg
from otaclient_common import _env
from otaclient_common._io import (
    ensure_private_dir,
    file_sha256,
    read_str_from_file,
    remove_file,
    write_str_to_file_atomic,
)
from otaclient_common.linux import subprocess_run_wrapper

logger = logging.getLogger(__name__)

APPLIED, PREVIOUS, DISCARD, STAGING = "applied", "previous", "discard", "staging"
RECORD_FNAME = "record.json"
RECORD_SCHEMA_VERSION = 1
"""The record's fields that others read (docs/overlay.md) keep their meaning
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
"""What was last recorded as `running`; `last-<event>-boot` for any event recorded
once per boot (`_once_per_boot`)."""
BASE_CHANGE_FNAME = "base-change-staged.json"
"""Written when a full update is staged while the slots share the store and it holds an
overlay: the slot that boots next may run the same image the overlay was applied on,
which the image ID alone cannot tell from the base it was applied on
(`whose_overlay`)."""
BOOT_ID_FPATH = "/proc/sys/kernel/random/boot_id"

STORE_MODE = 0o700
"""The store is root's alone; a directory made on the way to it is 0755
(`ensure_private_dir`)."""

MOUNT_OPTIONS = "ro,nosuid,nodev"
"""A slot may hold executables (an overlay may carry binaries and launch files), but
nothing in it runs with privilege it does not have, and it holds no device."""
MOUNT_COMMAND_TIMEOUT = 60
"""Seconds a mount command may take: the boot waits for the mounts, and must not hang
on one."""
STAGE_DPATH = "/run/otaclient-overlay"
"""Where, on the host, a slot's bind is made read-only before it goes to the slot's
path: a private tmpfs mounted for each bind and taken away after it (`bind_ro`)."""
STDERR_TAIL = 1024
"""How much of a failed command's error output is kept in its error, from the end."""

_FILES = TypeAdapter(list[OverlayFileEntry])


class OverlayFailed(Exception):
    """An overlay could not be put in place. `restored` tells whether the slots are back
    where they were (the overlay before, or the base's defaults), or could not be put
    back either."""

    def __init__(self, msg: str, *, restored: bool) -> None:
        super().__init__(msg)
        self.restored = restored


class CommandFailed(Exception):
    """A command otaclient runs for an overlay failed: what it was, how it ended
    and the end of what it wrote to stderr."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _boot_id() -> str:
    return read_str_from_file(BOOT_ID_FPATH, _default="")


def why_not_private(path: Path) -> str | None:
    """Why the store at `path` cannot be trusted, None when it can: from the mount
    point it is on down to it, every directory that exists is root's, not writable by
    group or others, and not a symbolic link (`ensure_private_dir`).

    An overlay's files are bound over the base and its `apply` runs as root: a directory
    on the way that another user can write (others write under /opt/data on some
    devices) would let that user rename the store away and plant an overlay of their
    own. What otaclient makes on the way (0755, the store 0700) passes."""
    try:
        ensure_private_dir(path, create=False)
    except Exception as e:
        return str(e) or repr(e)
    return None


def on_the_active_root_filesystem(path: Path) -> bool:
    """Whether `path` (the nearest of it and its parents that exists) is on the
    filesystem of the running slot's root: the host's root from an app image.

    A store there is the slot's own, and a full OTA writes the new slot without it; a
    store elsewhere (a partition of its own, mounted by both slots) is shared, and
    stays as it is across a full OTA."""
    _root = _env.get_dynamic_client_chroot_path() or cfg.CANONICAL_ROOT
    _at = Path(os.path.abspath(path))
    while not _at.exists() and _at != _at.parent:
        _at = _at.parent
    return os.stat(_at).st_dev == os.stat(_root).st_dev


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
    The slot was checked as an OverlaySlot when its overlay was applied."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    path: str
    apply: str

    @property
    def relpath(self) -> str:
        """The path as a tree relative to `/` names it."""
        return self.path[1:]

    @classmethod
    def of(cls, slot: OverlaySlot) -> RecordedSlot:
        return cls(name=slot.name, path=slot.path, apply=slot.apply)


class AppliedRecord(BaseModel):
    """An overlay as this slot applied it, what it was applied on, and the release that
    named it.

    `overlay_*` is the overlay itself. `firmware_version`, `release_*` and `image_id`
    are the release the request named, `image_id` the overlay image's. `base_image_id`
    is the running image's (its version file) when the overlay was applied, empty when
    it has none: with `image_id` it says that this release runs on this base.
    `base_version` and `base_version_detail` are what the slot recorded before any
    overlay, so that a dropped overlay reports the base again.

    `schema_version`, `image_id`, `overlay_name`, `overlay_version`, `base_image_id` and
    `applied_at` are read by others (docs/overlay.md).
    """

    schema_version: int = RECORD_SCHEMA_VERSION
    overlay_manifest_digest: str
    overlay_name: str
    overlay_version: str
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
                "overlay_manifest_digest",
                "overlay_name",
                "overlay_version",
                "base_image_id",
                "firmware_version",
                "release_name",
                "release_id",
                "image_id",
                "session_id",
            }
        ) | {"slots": [_s.name for _s in self.slots]}

    def describe(self) -> str:
        return (
            f"{self.overlay_name} {self.overlay_version} "
            f"({self.overlay_manifest_digest})"
        )

    def is_recorded_by(self, records: CurrentSlotRecords) -> bool:
        """Whether the current slot records this overlay's release as its own."""
        _version, _detail = records.load_recorded_version()
        return _version == self.firmware_version and (
            (_detail.image_id if _detail else "") == self.image_id
        )


class OverlaySlotMounter(Protocol):
    def is_mounted(self, mount_point: str) -> bool: ...
    def make_mount_point(self, mount_point: str) -> bool: ...
    def bind_ro(self, source: Path, mount_point: str) -> None: ...
    def umount(self, mount_point: str) -> None: ...
    def shows(self, mount_point: str, directory: Path) -> bool: ...


class ApplyRunner(Protocol):
    def __call__(self, command: str) -> None: ...


class CurrentSlotRecords(Protocol):
    """The version records of the A/B slot otaclient runs from (the boot
    controller's), not a parameter slot's."""

    def load_recorded_version(self) -> tuple[str, VersionDetail | None]: ...
    def restore_recorded_version(
        self, *, version: str, version_detail: VersionDetail | None
    ) -> None: ...


class HostOverlaySlotMounter:
    """Mounts in the host's mount namespace, also from an app image: what the
    device's processes see is the host's view, and so are the paths checked.

    The store is used at its path as it is: an app image binds the host's /opt at the
    same path (samples/otaclient.service_app,
    samples/otaclient-overlay.service_app).
    Each command is bounded by MOUNT_COMMAND_TIMEOUT.
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
                timeout=MOUNT_COMMAND_TIMEOUT,
            )
            return True
        except Exception:
            return False

    def shows(self, mount_point: str, directory: Path) -> bool:
        """Whether what is mounted at `mount_point` is `directory` (a bind of it): the
        same inode on the same device. False when either cannot be looked at."""
        try:
            _at = os.stat(self._on_host(mount_point))
            _dir = os.stat(directory)
        except OSError:
            return False
        return (_at.st_dev, _at.st_ino) == (_dir.st_dev, _dir.st_ino)

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

    def _run(self, cmd: list[str], *, what: str) -> None:
        run_command(
            cmd,
            what=what,
            set_host_mnt_ns=self._host_mnt_ns(),
            timeout=MOUNT_COMMAND_TIMEOUT,
        )

    def bind_ro(self, source: Path, mount_point: str) -> None:
        """Bind `source` at `mount_point`, read-only, nosuid and nodev.

        The bind is made in a private staging mount (STAGE_DPATH), made read-only
            there, and only then moved to the slot's path. A bind made at the slot's
            path reaches the peers of the mount it is under (a bind of the root, the
            view of a service or a container that receives the host's mounts) the
            moment it is made, writable, with setuid and devices, and the remount that
            makes it read-only does not reach them. A mount that is moved reaches them
            as it is. Nothing is left at the slot's path when a step fails.
        """
        self._on_host(mount_point)
        _staged = f"{STAGE_DPATH}/slot"
        if self.is_mounted(STAGE_DPATH):
            # left by an otaclient stopped in the middle of a bind
            self._run(
                ["umount", "--lazy", STAGE_DPATH], what="clearing the staging mount"
            )
        self._run(["mkdir", "-p", STAGE_DPATH], what="making the staging directory")
        self._run(
            ["mount", "-t", "tmpfs", "-o", "mode=0700,size=64k"]
            + ["otaclient-overlay", STAGE_DPATH],
            what="mounting the staging tmpfs",
        )
        try:
            # private before anything is mounted in it: the bind and its remount
            #   reach no peer
            self._run(
                ["mount", "--make-private", STAGE_DPATH],
                what="making the staging mount private",
            )
            self._run(["mkdir", _staged], what="making the staging mount point")
            self._run(
                ["mount", "--bind", str(source), _staged],
                what=f"binding {mount_point}",
            )
            self._run(
                ["mount", "-o", f"remount,bind,{MOUNT_OPTIONS}", _staged],
                what=f"making {mount_point} read-only",
            )
            self._run(
                ["mount", "--move", _staged, mount_point],
                what=f"moving the read-only bind to {mount_point}",
            )
        finally:
            # with the bind when it did not move: nothing left bound writable
            with contextlib.suppress(Exception):
                self._run(
                    ["umount", "--lazy", STAGE_DPATH],
                    what="unmounting the staging tmpfs",
                )

    def umount(self, mount_point: str) -> None:
        # lazy: a program that runs from the slot's files (what an overlay holds)
        #   keeps them until it exits, rather than the slot staying as it was
        self._run(["umount", "--lazy", mount_point], what=f"unmounting {mount_point}")


def run_apply(command: str) -> None:
    """Run a slot's `apply` in the host's root and mount namespace: what it runs and
    reads is what the device's processes see, the slots' mounts included (an `apply`
    the overlay carries is a file on one of them), also from an app image."""
    logger.info(f"running {command} ...")
    run_command(
        [command],
        what=f"apply {command}",
        # entering it resets the root to the host's: no chroot as well, as the
        #   mounter does
        set_host_mnt_ns=_env.is_dynamic_client_running(),
        timeout=cfg.OVERLAY_APPLY_TIMEOUT,
    )


def _file_key(entry: OverlayFileEntry) -> tuple[str, int, str]:
    """What tells two files of an overlay apart: path, size and digest."""
    return (entry.path, entry.size, str(entry.digest))


def _slot_files(
    files: Iterable[OverlayFileEntry], slot: RecordedSlot
) -> frozenset[tuple[str, int, str]]:
    return frozenset(
        _file_key(_f) for _f in files if _f.path.startswith(slot.relpath + "/")
    )


def _slot_key(slot: RecordedSlot) -> tuple[str, str]:
    """A slot is the same in two overlays when its name and its path are."""
    return (slot.name, slot.path)


class OverlayStore:
    """The store, the mounts and the history."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        mounter: OverlaySlotMounter | None = None,
        runner: ApplyRunner | None = None,
    ) -> None:
        self.root = root or Path(cfg.OVERLAY_DPATH)
        self._mounter = mounter or HostOverlaySlotMounter()
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

    def tree_of(self, slot: RecordedSlot, overlay_dir: Path | None = None) -> Path:
        """The unpacked directory of `slot` in the overlay in `overlay_dir`
        (applied/)."""
        return (overlay_dir or self.applied_dir) / TREE_DNAME / slot.relpath

    # ------ records ------ #

    def applied_record(self) -> AppliedRecord | None:
        """The applied overlay's record; None when there is none, or when it cannot be
        read (`check_at_startup` drops such an overlay)."""
        try:
            return self._read_record(self.applied_dir)
        except Exception as e:
            logger.warning(f"the applied overlay's record is unreadable: {e!r}")
            return None

    @staticmethod
    def _read_record(overlay_dir: Path) -> AppliedRecord | None:
        try:
            _raw = (overlay_dir / RECORD_FNAME).read_text()
        except FileNotFoundError:
            return None
        return AppliedRecord.model_validate_json(_raw)

    @staticmethod
    def _read_files(overlay_dir: Path) -> list[OverlayFileEntry]:
        try:
            return _FILES.validate_json((overlay_dir / FILES_FNAME).read_text())
        except FileNotFoundError:
            return []

    # ------ whose the overlay is ------ #

    def why_not_trusted(self) -> str | None:
        """Why the store cannot be trusted (`why_not_private`), None when it can:
        nothing is laid from it, stored into it, recorded in it or deleted from it
        otherwise. Checked from the mount point the store is on, which an app image
        sees as the host does (it binds the host's /opt recursively)."""
        return why_not_private(self.root)

    def is_shared(self) -> bool:
        """Whether the store is on another filesystem than the running slot's root: a
        partition both slots mount, which may hold the overlay another base applied."""
        return not on_the_active_root_filesystem(self.root)

    def base_change_staged(self) -> bool:
        """Whether a full update was staged since the overlay was applied, the slots
        sharing the store (`BASE_CHANGE_FNAME`)."""
        return os.path.lexists(self.root / BASE_CHANGE_FNAME)

    def whose_overlay(
        self,
        running_image_id: str,
        *,
        slot_records: CurrentSlotRecords | None = None,
    ) -> tuple[AppliedRecord | None, str | None]:
        """The overlay the store holds (the applied one, else the one an unfinished
        switch left in `previous/`), and why it is not the running base's, None when it
        is.

        It is not when its record cannot be read, when it was applied on another
        image, when it was applied on an image without an ID while the slots share the
        store (two such bases cannot be told apart), or when a full update was staged
        since (the same image may have been installed again in the other slot) and
        the slot does not record the overlay's release: the slot it was applied on still
        does. Without `slot_records` (at boot), a staged full update is enough: who
        runs is told at otaclient's start."""
        try:
            _overlay = self._read_record(self.applied_dir) or self._read_record(
                self.previous_dir
            )
        except Exception as e:
            return None, f"the overlay's record is unreadable: {e!r}"
        if _overlay is None:
            return None, None
        if _overlay.base_image_id != running_image_id:
            return _overlay, (
                f"the overlay was applied on image {_overlay.base_image_id!r}, the "
                f"slot runs image {running_image_id!r}"
            )
        if not _overlay.base_image_id and self.is_shared():
            return _overlay, (
                "the overlay was applied on an image without an image ID, and the "
                "slots share the store: which base it was applied on cannot be told"
            )
        if self.base_change_staged():
            if slot_records is None:
                return _overlay, (
                    "a full update was staged since the overlay was applied: which "
                    "slot runs is told when otaclient starts"
                )
            if not _overlay.is_recorded_by(slot_records):
                return _overlay, (
                    "a full update was staged since the overlay was applied, and this "
                    "slot does not record the overlay's release: it runs the new base"
                )
        return _overlay, None

    def applied_record_of(
        self, running_image_id: str, *, slot_records: CurrentSlotRecords
    ) -> AppliedRecord | None:
        """The applied overlay's record when the overlay is the running base's
        (`whose_overlay`): what the overlay before an update is, and what it covers;
        None otherwise."""
        if (
            self.whose_overlay(running_image_id, slot_records=slot_records)[1]
            is not None
        ):
            return None
        return self.applied_record()

    def set_aside_another_bases_overlay(
        self, running_image_id: str, *, slot_records: CurrentSlotRecords
    ) -> None:
        """Before a switch, the running base committed: an overlay in the store that is
        not the running base's (`whose_overlay`) is not the overlay before. It is
        dropped as at otaclient's start, nothing unmounted: the switch is then a first
        overlay's, every slot of the new overlay applied, and a failure undoes it to the
        base's defaults, never to the other base's overlay."""
        _overlay, _why = self.whose_overlay(running_image_id, slot_records=slot_records)
        if _why is not None:
            self._drop_another_bases(
                _overlay,
                f"replaced by an overlay on image {running_image_id!r}: {_why}",
            )

    def record_event(self, event: str, **fields: Any) -> None:
        """Append one line of evidence. Never raises: the history is a record of
        what happened, not a condition for it. Nothing is written into a store that
        is not root's alone."""
        _line = json.dumps({"time": _now(), "event": event, **fields}, sort_keys=True)
        if (_why := self.why_not_trusted()) is not None:
            logger.warning(f"not recording {event}: the store is not trusted ({_why})")
            return
        try:
            ensure_private_dir(self.root, mode=STORE_MODE)
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
        ensure_private_dir(self.root, mode=STORE_MODE)
        self.staging_dir.mkdir(mode=0o700)
        return self.staging_dir

    def discard_staging(self) -> None:
        shutil.rmtree(self.staging_dir, ignore_errors=True)

    def stage(self, record: AppliedRecord, files: list[OverlayFileEntry]) -> None:
        """Write the staged overlay's record and the files that were unpacked for it,
        and flush the staging onto the disk: what is switched to is on the disk."""
        write_str_to_file_atomic(
            self.staging_dir / FILES_FNAME, _FILES.dump_json(files).decode()
        )
        write_str_to_file_atomic(
            self.staging_dir / RECORD_FNAME, record.model_dump_json()
        )
        os.sync()

    # ------ mounts and applies ------ #

    def _unmount(self, mount_points: Iterable[str]) -> None:
        for _point in dict.fromkeys(mount_points):
            while self._mounter.is_mounted(_point):
                # lazily, with whatever is mounted below it: said, as it goes
                logger.info(f"taking the mount at {_point} away")
                self._mounter.umount(_point)

    def _set_aside_applied(self) -> None:
        """Rename applied/ to discard/ (one rename, so that the overlay is not the
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
        """Mount `record`, the applied overlay by now, and run the `apply` of `changed`.
        The renames that made it the applied one are flushed before an `apply` runs:
        a boot from here on mounts what an `apply` took effect with."""
        self._mount(record)
        os.sync()
        return self._run_applies(changed)

    @staticmethod
    def changed_slots(
        old: AppliedRecord | None,
        old_files: list[OverlayFileEntry],
        new: AppliedRecord,
        new_files: list[OverlayFileEntry],
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
        new_files: list[OverlayFileEntry],
    ) -> list[RecordedSlot]:
        """The slots `new` fills whose contents change from the overlay in `old_dir`: by
        their file lists, unless the files in `old_dir` are not its overlay's own, when
        every slot `new` fills changes."""
        if old is not None and (_why := self.check_tree(old, old_dir)) is not None:
            logger.warning(
                f"the files of {old.describe()} are not the overlay's ({_why}): "
                "every slot is applied"
            )
            return list(new.slots)
        return self.changed_slots(old, self._read_files(old_dir), new, new_files)

    def switch(self) -> bool:
        """Put the staged overlay in place of the applied one, keeping that one as
        `previous/` until `commit`, and run the `apply` of every slot the new overlay
        fills whose contents change; nothing runs for a slot it takes away.

        On failure the applied overlay is put back, its `apply`s run again, and
        OverlayFailed raised, saying whether that worked. Returns True when a
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
            # the new overlay's slots too: a switch that failed before may have left one
            #   (anything else mounted there was refused before the update began)
            self._unmount(self._paths(_old, _new))
            if self.applied_dir.exists():
                os.replace(self.applied_dir, self.previous_dir)
            _renamed = True
            os.replace(self.staging_dir, self.applied_dir)
            return self._put_in_place(_new, _changed)
        except Exception as e:
            logger.error(f"failed to put the overlay in place: {e}; rolling back")
            if _renamed:
                _restored = self.roll_back(cause=e)
            else:
                # nothing was moved: the applied overlay is still the old one, only its
                #   mounts may be gone
                _restored = self._remount(_old, cause=e)
            raise OverlayFailed(
                f"the overlay was not put in place: {e}", restored=_restored
            ) from e

    def _remount(self, record: AppliedRecord | None, *, cause: Exception) -> bool:
        try:
            self._mount(record)
            self.record_event("rolled_back", cause=str(cause), remounted=True)
            return True
        except Exception as e:
            logger.error(f"failed to mount the applied overlay again: {e}")
            self.record_event("roll_back_failed", cause=str(cause), error=str(e))
            return False

    def commit(self) -> None:
        """The slot records the new overlay's release: the one before goes for good.

        One rename marks it gone before it is deleted: a crash while it is deleted
        leaves `discard/`, not a `previous/` that would be taken for a switch that did
        not finish. Never raises: the update has succeeded by now.
        """
        try:
            if self.previous_dir.exists():
                shutil.rmtree(self.discard_dir, ignore_errors=True)
                os.replace(self.previous_dir, self.discard_dir)
        except Exception as e:
            logger.warning(f"failed to set the overlay before aside: {e!r}")
        shutil.rmtree(self.discard_dir, ignore_errors=True)
        shutil.rmtree(self.previous_dir, ignore_errors=True)
        if (_record := self.applied_record()) is not None:
            self.record_event("applied", **_record.evidence())

    def roll_back(self, *, cause: Exception, unmounted: Iterable[str] = ()) -> bool:
        """Put `previous/` (or no overlay, when there was none) back in place of the
        applied overlay, and run the `apply` of the slots the overlay put back fills
        that change back; nothing runs for a slot it does not fill. Never raises:
        returns whether the slots are back, a failure is recorded.

        `unmounted` are the paths no overlay was mounted at when the roll back began
        (the boot mounts nothing while a switch is unfinished): what reads them started
        with the base's, so a slot the overlay put back fills there is applied too.

        The renames come first, the mounts after: whatever fails below, `applied/` is
        the overlay put back, and that is what a later boot and otaclient's next start
        lay, not the failed one."""
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
                #   or no overlay before): every slot the overlay put back fills changes
                _changed = list(_back.slots) if _back else []
            _unmounted = set(unmounted)
            _changed += [
                _s
                for _s in (_back.slots if _back else [])
                if _s.path in _unmounted and _s not in _changed
            ]
            _points = self._paths(_failed, _back)
            # the failed overlay leaves applied/ (for discard/), and the overlay put
            #   back (or none) takes its place
            self._set_aside_applied()
            if self.previous_dir.exists():
                os.replace(self.previous_dir, self.applied_dir)
            self._unmount(_points)
            shutil.rmtree(self.discard_dir, ignore_errors=True)
            _reboot = self._put_in_place(_back, _changed)
            if _unmounted:
                self._reboot_required(_reboot, "rolled_back")
            self.record_event(
                "rolled_back",
                cause=str(cause),
                overlay=_back.overlay_manifest_digest if _back else None,
            )
            return True
        except Exception as e:
            logger.error(f"failed to roll the overlay back: {e}")
            self.record_event("roll_back_failed", cause=str(cause), error=str(e))
            return False

    def _complete_switch(self, *, unmounted: Iterable[str] = ()) -> None:
        """A switch whose release the slot already records, crashed before its commit
        finished: its mounts are put back from applied/ (a slot bound before the
        rename sees previous/), then it is committed.

        The switch ran the `apply`s; a slot at one of the `unmounted` paths (the boot
        mounts nothing while a switch is unfinished) was read with the base's since,
        and its `apply` runs again once the switch is committed."""
        _applied = self.applied_record()
        self._unmount(self._paths(_applied))
        self._put_in_place(_applied, ())
        self.commit()
        _unmounted = set(unmounted)
        if _applied is not None and (
            _again := [_s for _s in _applied.slots if _s.path in _unmounted]
        ):
            self._apply_again(_applied, _again, after="recovered")

    # ------ the applied overlay, verified ------ #

    def check_tree(
        self, record: AppliedRecord, overlay_dir: Path | None = None
    ) -> str | None:
        """Why the unpacked files of the overlay in `overlay_dir` (the applied one by
        default) are not what otaclient unpacked and checked, or None: every file of its
        list, with its size and digest, and nothing else."""
        overlay_dir = overlay_dir or self.applied_dir
        try:
            _expected = {_f.path: _f for _f in self._read_files(overlay_dir)}
        except Exception as e:
            return f"the list of the overlay's files is unreadable: {e!r}"
        _tree = overlay_dir / TREE_DNAME
        _found: set[str] = set()
        for _slot in record.slots:
            _slot_dir = self.tree_of(_slot, overlay_dir)
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
                        return f"{_rel} is not in the overlay"
                    if _want.size != _st.st_size or _want.digest != Sha256Digest(
                        file_sha256(_p)
                    ):
                        return f"{_rel} differs from the overlay"
                    _found.add(_rel)
        if _missing := sorted(set(_expected) - _found):
            return (
                f"{len(_missing)} files of the overlay are missing, {_missing[0]} first"
            )
        return None

    # ------ at boot ------ #

    def mount_at_boot(self, running_image_id: str) -> bool:
        """At boot, before local-fs.target (`otaclient mount-overlay`): bind
        each slot of the applied overlay that is not mounted over its path, read-only,
        before anything that reads it starts. The mount point is made when the image
        has none and it can be made there.

        Only what `applied/` says is mounted, and only when it holds: what is not
        decided here is left to otaclient's start (`check_at_startup`), which has the
        slot's records to decide by. Nothing is laid from a store that is not root's
        alone, nor an overlay this boot cannot tell is the running base's
        (`whose_overlay` without the slot's records: a trial boot of a new base may run
        over the other base's overlay in a store the slots share), nor an overlay whose
        record cannot be read or is of a newer schema than this otaclient's: those are
        logged and the store is left as it is. Nothing is mounted while a switch is
        unfinished (`previous/`: otaclient completes it or rolls it back). No `apply`
        runs: what reads a slot has not started yet. One event is recorded when the
        overlay is the running base's, none otherwise.

        Returns False when a slot of the overlay, or the overlay itself, could not be
        mounted; a slot that cannot be mounted does not keep the others from being
        mounted. Never raises.
        """
        _boot = _boot_id()
        try:
            if not (
                os.path.lexists(self.applied_dir) or os.path.lexists(self.previous_dir)
            ):
                logger.info("no overlay is applied")
                return True
            if (_untrusted := self.why_not_trusted()) is not None:
                logger.error(
                    f"the overlay store is not root's alone ({_untrusted}): "
                    "nothing is mounted"
                )
                return False
            _overlay, _why = self.whose_overlay(running_image_id)
            if _overlay is None:
                if _why is None:
                    logger.info("no overlay is applied")
                    return True
                logger.error(
                    f"{_why}: nothing is mounted, otaclient drops the overlay once "
                    "this base is committed"
                )
                return False
            if _why is not None:
                logger.warning(
                    f"overlay {_overlay.describe()} is not laid ({_why}): the store "
                    "is left as it is, otaclient decides when it starts, and drops "
                    "the overlay once this base is committed if it is not this base's"
                )
                return True
            if _overlay.schema_version > RECORD_SCHEMA_VERSION:
                logger.warning(
                    f"overlay {_overlay.describe()} was recorded with schema "
                    f"{_overlay.schema_version}, newer than this otaclient's "
                    f"{RECORD_SCHEMA_VERSION}: nothing is mounted, left to "
                    "otaclient's start"
                )
                return True
            _record = self._read_record(self.applied_dir)
            if self.previous_dir.exists() or _record is None:
                logger.warning(
                    "an overlay switch did not finish: nothing is mounted, "
                    "otaclient completes it or rolls it back when it starts"
                )
                self.record_event(
                    "not_mounted_at_boot",
                    reason="a switch did not finish",
                    boot_id=_boot,
                    overlay_manifest_digest=_overlay.overlay_manifest_digest,
                )
                return True
        except Exception as e:
            logger.error(f"failed to read the overlay store: {e!r}")
            return False

        _mounted: list[str] = []
        _already: list[str] = []
        _made: list[str] = []
        _failed: dict[str, str] = {}
        for _slot in _record.slots:
            try:
                if self._mounter.is_mounted(_slot.path):
                    _already.append(_slot.name)
                    continue
                if self._mounter.make_mount_point(_slot.path):
                    logger.info(
                        f"made {_slot.path}, the mount point of slot {_slot.name}"
                    )
                    _made.append(_slot.path)
                self._mounter.bind_ro(self.tree_of(_slot), _slot.path)
                logger.info(f"slot {_slot.name} is bound at {_slot.path}")
                _mounted.append(_slot.name)
            except Exception as e:
                logger.error(
                    f"slot {_slot.name} could not be mounted at {_slot.path}: {e}"
                )
                _failed[_slot.name] = str(e)
        self.record_event(
            "mounted_at_boot",
            boot_id=_boot,
            overlay_manifest_digest=_record.overlay_manifest_digest,
            mounted=_mounted,
            already_mounted=_already,
            mount_points_made=_made,
            failed=_failed,
        )
        return not _failed

    # ------ at otaclient's start ------ #

    def check_at_startup(
        self,
        running_image_id: str,
        *,
        slot_records: CurrentSlotRecords,
        base_committed: bool,
    ) -> AppliedRecord | None:
        """At otaclient's start: the applied overlay, kept as the last update left it.

        `running_image_id` is the running image's ID from its version file, "" when
        it has none (or none otaclient reads), as `base_image_id` was recorded.
        `base_committed` is whether the running base is committed (the slot booted at
        SUCCESS or INITIALIZED, otaclient not the downloaded client), as the boot
        controller concluded the boot.

        The boot-time unit (`mount_at_boot`) mounted the overlay at boot, before
        anything read it; this is where it is checked, and where that is recorded. A
        switch that did not finish (`previous/` still there) is completed when the slot
        records the applied overlay's release, and rolled back otherwise. Files other
        than the overlay's are kept and recorded: what the ECU runs changes by an update
        only. A slot that is not mounted (the boot-time unit is not installed, could not
        mount it, or left it to this start after a full update was staged) is mounted,
        and its `apply` runs.

        An overlay that is not the running base's (`whose_overlay`), or whose record
        cannot be read, is not laid. It is dropped once the running base is committed,
        with nothing unmounted (it was never laid on this base); until then (a trial
        boot of a new base that may still fall back to the base the overlay was applied
        on) it is kept as it is, recorded once per boot. That is for a store the slots
        share only: a store on the slot's own root holds no overlay of a base the ECU
        can fall back to, and its overlay of another base is dropped whatever the
        status. Otherwise the overlay is dropped (the slots get the base's defaults, the
        slot reports the base) only when a slot cannot be mounted; an `apply` that fails
        after a slot was mounted again is recorded, the overlay kept. Nothing is laid,
        recorded or deleted when the store is not root's alone.
        """
        if (_untrusted := self.why_not_trusted()) is not None:
            logger.error(
                f"the overlay store is not root's alone ({_untrusted}): "
                "nothing is laid from it, and nothing deleted"
            )
            return None
        # whose overlay the store holds, before anything in it changes
        _overlay, _why = self.whose_overlay(running_image_id, slot_records=slot_records)
        if _why is not None:
            # only a store the slots share may hold the overlay of a base the ECU can
            #   fall back to: one on this slot's own root came with this slot
            if base_committed or not self.is_shared():
                self._drop_another_bases(_overlay, _why)
            else:
                self._keep_for_its_base(_overlay, _why)
            return None
        _because = "the boot-time unit (otaclient mount-overlay) did not mount them"
        if self.base_change_staged():
            # the slot the overlay was applied on: the full update staged from it did
            #   not switch, or fell back; the boot left the overlay to this start
            logger.warning(
                "a full update was staged since the overlay was applied, and "
                "this slot still records the overlay's release: laid again"
            )
            remove_file(self.root / BASE_CHANGE_FNAME)
            self.record_event(
                "base_change_not_taken",
                overlay_manifest_digest=_overlay.overlay_manifest_digest
                if _overlay
                else None,
            )
            _because = "a full update was staged, the boot left them to this start"

        shutil.rmtree(self.discard_dir, ignore_errors=True)
        if self.previous_dir.exists():
            self._finish_switch(slot_records)

        try:
            _record = self._read_record(self.applied_dir)
        except Exception as e:
            self._drop(None, f"the overlay's record is unreadable: {e!r}")
            return None
        if _record is None:
            return None
        logger.info(
            f"overlay {_record.describe()} is applied on image "
            f"{_record.base_image_id or '(no version file)'}"
        )
        if (_mismatch := self.check_tree(_record)) is not None:
            logger.error(
                f"the files of {_record.describe()} are not the overlay's "
                f"({_mismatch}): kept as they are until the next update of the overlay"
            )

        _unmounted = [
            _s for _s in _record.slots if not self._mounter.is_mounted(_s.path)
        ]
        if _unmounted and not self._mount_again(
            _record, _unmounted, slot_records=slot_records, because=_because
        ):
            return None

        self._record_running(_record, _mismatch)
        return _record

    def _keep_for_its_base(self, record: AppliedRecord | None, why: str) -> None:
        """An overlay not laid, kept for the base it may be (a trial boot not
        committed): said each start, recorded once per boot."""
        _what = record.describe() if record else "the applied overlay"
        logger.warning(
            f"{_what} is not laid ({why}): kept as it is until this base is committed"
        )
        _digest = record.overlay_manifest_digest if record else ""
        if self._once_per_boot("not-laid", _digest):
            self.record_event(
                "not_laid",
                reason=why,
                boot_id=_boot_id(),
                **(record.evidence() if record else {}),
            )

    def _once_per_boot(self, name: str, key: str) -> bool:
        """Whether `key` is new in this boot for `name` (`last-<name>-boot`), noted
        when it is: what is recorded once per boot is not recorded again by an
        otaclient that restarts."""
        _boot = _boot_id()
        _key = " ".join([_boot, *key.split()]) if key else _boot
        _file = self.root / f"last-{name}-boot"
        if read_str_from_file(_file, _default="") == _key:
            return False
        try:
            write_str_to_file_atomic(_file, _key)
        except Exception as e:
            logger.warning(f"failed to note {name} for this boot: {e!r}")
        return True

    def _drop_another_bases(self, record: AppliedRecord | None, why: str) -> None:
        """Drop an overlay that is not the running base's, the running base committed:
        the overlay and a switch it left unfinished leave the store, and so does a
        staged full update's mark. It is not laid on this base at boot, so what is
        mounted at its slots' paths is this base's own and stays; only a bind of the
        overlay's own directory (laid in this boot, before the running image changed in
        place) is taken away, once."""
        _what = record.describe() if record else "the applied overlay"
        logger.warning(f"{_what} is not this base's ({why}): dropped")
        _evidence = record.evidence() if record else {}
        _ours: list[str] = []
        for _slot in record.slots if record else []:
            try:
                if self._mounter.is_mounted(_slot.path) and self._mounter.shows(
                    _slot.path, self.tree_of(_slot)
                ):
                    _ours.append(_slot.path)
            except Exception as e:
                logger.warning(f"cannot tell what is mounted at {_slot.path}: {e!r}")
        try:
            self._set_aside_applied()
            for _point in _ours:
                logger.info(f"taking the overlay's own mount at {_point} away")
                self._mounter.umount(_point)
            shutil.rmtree(self.discard_dir, ignore_errors=True)
            shutil.rmtree(self.previous_dir, ignore_errors=True)
            remove_file(self.root / BASE_CHANGE_FNAME)
        except Exception as e:
            logger.error(f"failed to drop {_what}: {e!r}")
            self.record_event("drop_failed", reason=why, error=str(e), **_evidence)
            return
        self.record_event("dropped", reason=why, **_evidence)

    def _finish_switch(self, slot_records: CurrentSlotRecords) -> None:
        try:
            _applied = self._read_record(self.applied_dir)
        except Exception:
            _applied = None
        try:
            _before = self._read_record(self.previous_dir)
        except Exception:
            _before = None
        # the boot mounts nothing while a switch is unfinished: after a reboot, what
        #   reads these slots started with the base's
        _unmounted = [
            _p
            for _p in self._paths(_applied, _before)
            if not self._mounter.is_mounted(_p)
        ]
        if _applied is not None and _applied.is_recorded_by(slot_records):
            logger.warning(
                f"the switch to {_applied.describe()} was recorded but not committed: "
                "completing it"
            )
            self._complete_switch(unmounted=_unmounted)
            self.record_event("recovered", completed=True)
            return
        logger.warning("an overlay switch did not finish: rolling it back")
        if self.roll_back(
            cause=RuntimeError("an unfinished switch"), unmounted=_unmounted
        ):
            self.record_event("recovered", completed=False)

    def _mount_again(
        self,
        record: AppliedRecord,
        unmounted: list[RecordedSlot],
        *,
        slot_records: CurrentSlotRecords,
        because: str,
    ) -> bool:
        """Slots of the applied overlay that are not mounted (`because`: the boot-time
        unit is not installed, failed for them, or left them to this start), so they
        ran with the base's defaults until now. They are mounted and their `apply`
        runs again; a slot that cannot be mounted drops the overlay (False), an `apply`
        that fails is recorded and the overlay kept (True)."""
        logger.warning(
            f"slots {[_s.name for _s in unmounted]} are not mounted ({because}): "
            "mounting them and running their apply"
        )
        try:
            # the slots not mounted are `unmounted`: _mount lays those only, making
            #   a mount point that has gone
            self._mount(record)
        except Exception as e:
            self._drop(
                record,
                f"the overlay could not be mounted: {e}",
                slot_records=slot_records,
            )
            return False
        self.record_event(
            "remounted",
            overlay=record.overlay_manifest_digest,
            slots=[_s.name for _s in unmounted],
        )
        self._apply_again(record, unmounted, after="remounted")
        return True

    def _apply_again(
        self, record: AppliedRecord, slots: list[RecordedSlot], *, after: str
    ) -> None:
        """Run the `apply` of `slots`, mounted at otaclient's start: one that fails is
        recorded, the overlay kept."""
        try:
            self._reboot_required(self._run_applies(slots), after)
        except Exception as e:
            logger.error(f"the apply of the slots mounted again failed: {e}")
            self.record_event(
                "apply_failed", overlay=record.overlay_manifest_digest, error=str(e)
            )

    def _record_running(self, record: AppliedRecord, mismatch: str | None) -> None:
        """`running` once per boot, `tree_mismatch` once per boot and finding: an
        otaclient restarting in a loop adds no lines."""
        _boot = _boot_id()
        if self._once_per_boot("running", ""):
            self.record_event("running", boot_id=_boot, **record.evidence())
        if mismatch is not None and self._once_per_boot("tree-mismatch", mismatch):
            self.record_event(
                "tree_mismatch",
                reason=mismatch,
                boot_id=_boot,
                overlay=record.overlay_manifest_digest,
            )

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
        """Drop the applied overlay: the slot reports the base again (when its records
        are still this overlay's release), the overlay leaves `applied/`, its mounts go,
        then the unpacked files. No `apply` runs: the slots go back to the base's when
        what reads them restarts.

        Without a `record` to go by (unreadable, or not this slot's), the slot's
        records are left as they are; what is unmounted is `mount_points` (none for
        an unreadable record: the boot mounts none of it), and what the history says
        of the overlay is `evidence`."""
        _what = record.describe() if record else "the applied overlay"
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
            _points = [*self._paths(record), *(mount_points or [])]
            # first, by a rename, the overlay leaves applied/: whatever fails below,
            #   neither a later boot nor otaclient's next start lays it again
            self._set_aside_applied()
            self._unmount(_points)
            shutil.rmtree(self.discard_dir, ignore_errors=True)
        except Exception as e:
            logger.error(f"failed to drop {_what}: {e}")
            self.record_event("drop_failed", reason=why, error=str(e), **_evidence)
            return
        self.record_event("dropped", reason=why, **_evidence)

    # ------ at a full OTA ------ #

    def carry_history_to(self, standby_root: Path | None, *, to_version: str) -> None:
        """At a full update, which writes a new slot from an image: the applied overlay
        stays behind, and the history says so. Never raises.

        A store on the running slot's own root filesystem is not in the new slot: the
        history is copied into `standby_root`, with that line, unless the way to the
        store there is not root's alone (a link the new image has on it, say). A store
        elsewhere (a partition of its own, shared by the slots), or a new slot written
        as an image (no `standby_root`), keeps the overlay: the line is added where it
        is (`shared`), and when the store holds an overlay, so is the mark that a full
        update was staged (`BASE_CHANGE_FNAME`): the new slot may run the same image,
        and only the slot's records then tell it from the one the overlay was applied on
        (`whose_overlay`)."""
        try:
            _record = self.applied_record()
            if _record is None and not self.history_file.exists():
                return  # no overlay was ever applied here: nothing to carry
            _left_behind = {
                "reason": f"full OTA to {to_version}",
                "overlay": _record.overlay_manifest_digest if _record else None,
                "overlay_version": _record.overlay_version if _record else None,
            }
            if standby_root is None or self.is_shared():
                if _record is not None and self.why_not_trusted() is None:
                    write_str_to_file_atomic(
                        self.root / BASE_CHANGE_FNAME,
                        json.dumps(
                            {
                                "boot_id": _boot_id(),
                                "to_version": to_version,
                                "time": _now(),
                            }
                        ),
                    )
                self.record_event("left_behind", shared=True, **_left_behind)
                return
            _dst = standby_root / Path(cfg.OVERLAY_DPATH).relative_to("/")
            try:
                ensure_private_dir(_dst, mode=STORE_MODE)
            except PermissionError as e:
                logger.warning(f"not carrying the overlay history: {e}")
                return
            for _fname in (HISTORY_ROTATED_FNAME, HISTORY_FNAME):
                if (self.root / _fname).exists():
                    shutil.copyfile(self.root / _fname, _dst / _fname)
            OverlayStore(root=_dst).record_event(
                "left_behind", shared=False, **_left_behind
            )
        except Exception as e:
            logger.warning(f"failed to carry the overlay history: {e!r}")


def mount_overlay_command() -> int:
    """`otaclient mount-overlay`: lay the applied overlay at boot.

    Run by a oneshot unit ordered before local-fs.target
    (samples/otaclient-overlay.service_app), in the app image as otaclient runs there:
    mounted in the host's namespace, the host's root seen at its chroot path. It needs
    the store and the running image's version file only: no gRPC and no boot controller,
    and it bounds each command it runs. Importing otaclient's configuration still parses
    ecu_info.yaml and proxy_info.yaml, which it does not use: before /boot is mounted,
    that logs that they were not found. Returns the exit code: 0 when every slot of the
    applied overlay is mounted (or nothing is to be), 1 when one is not. Never raises.
    """
    logging.basicConfig(level=logging.INFO, format=cfg.LOG_FORMAT)
    try:
        # imported here: the version file module imports this one (it reads the
        #   store), so a module-level import would be a cycle
        from otaclient.boot_control._image_version_file import running_image_id

        _ok = OverlayStore().mount_at_boot(running_image_id())
    except Exception as e:
        logger.exception(f"failed to mount the overlay: {e!r}")
        return 1
    return 0 if _ok else 1
