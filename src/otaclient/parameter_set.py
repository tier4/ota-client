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
      manifest.json, config.json   the set's own metadata, as downloaded
      tree/<path>   the slots' files, at their paths relative to /
    previous/       the set before, while a switch is under way
    staging/        a set being prepared
    history.jsonl   the evidence: one JSON object per line, per event

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
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from ota_image_libs.common.oci_spec import Sha256Digest
from ota_image_libs.v1.image_config.schema import ImageConfig
from ota_image_libs.v1.parameter_set.schema import ParameterSetConfig
from ota_image_libs.v1.parameter_set.slots import APPLY_REBOOT, ParameterSlot
from pydantic import BaseModel

from otaclient.configs.cfg import cfg
from otaclient_common import _env
from otaclient_common._io import write_str_to_file_atomic
from otaclient_common.linux import subprocess_run_wrapper

logger = logging.getLogger(__name__)

APPLIED, PREVIOUS, STAGING = "applied", "previous", "staging"
RECORD_FNAME = "record.json"
MANIFEST_FNAME = "manifest.json"
CONFIG_FNAME = "config.json"
TREE_DNAME = "tree"
HISTORY_FNAME = "history.jsonl"

FSTAB_BEGIN = "# BEGIN otaclient parameter set (managed by otaclient, do not edit)"
FSTAB_END = "# END otaclient parameter set"
FSTAB_OPTIONS = "bind,ro,nofail"


class ParameterSetFailed(Exception):
    """A set could not be put in place; the slot is back where it was."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#
# ------ the running base ------ #
#


class RunningBase(BaseModel):
    """The base payload this slot was installed from, as a full OTA kept it."""

    manifest_digest: Sha256Digest
    slots: list[ParameterSlot]


def load_running_base(image_meta_dir: Path) -> RunningBase | None:
    """The running base's manifest digest and slots, from what a full OTA saved in
    image-meta; None when it saved nothing (an otaclient before parameter sets, or a
    legacy OTA image)."""
    try:
        _manifest = (image_meta_dir / cfg.IMAGE_META_MANIFEST_FNAME).read_bytes()
        _config = ImageConfig.parse_metafile(
            (image_meta_dir / cfg.IMAGE_META_CONFIG_FNAME).read_text()
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

    `base_image_id` is the running image's ID (its version file) when the set was
    applied: the release's own `image_id` is the parameter image's, and the two
    together say that this release runs on this base. `base_version` and
    `base_version_detail` are what the slot recorded before any set, so that a
    dropped set reports the base again.
    """

    set_manifest_digest: str
    name: str
    version: str
    slots: list[ParameterSlot]
    base_manifest_digest: str
    base_image_id: str = ""
    firmware_version: str = ""
    release_name: str = ""
    release_id: str = ""
    image_id: str = ""
    base_version: str = ""
    base_version_detail: dict[str, str] | None = None
    applied_at: str = ""
    session_id: str = ""

    def evidence(self) -> dict[str, Any]:
        """What the history says of it."""
        return self.model_dump(
            include={
                "set_manifest_digest",
                "name",
                "version",
                "base_manifest_digest",
                "base_image_id",
                "firmware_version",
                "release_name",
                "release_id",
                "image_id",
                "session_id",
            }
        ) | {"slots": [_s.name for _s in self.slots]}


class SlotMounter(Protocol):
    def is_mounted(self, mount_point: str) -> bool: ...
    def bind_ro(self, source: Path, mount_point: str) -> None: ...
    def umount(self, mount_point: str) -> None: ...


class ApplyRunner(Protocol):
    def __call__(self, command: str) -> None: ...


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
        self._run(["mount", "--bind", str(source), mount_point])
        self._run(["mount", "-o", "remount,bind,ro", mount_point])

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
    def staging_dir(self) -> Path:
        return self.root / STAGING

    @property
    def history_file(self) -> Path:
        return self.root / HISTORY_FNAME

    # ------ records ------ #

    def applied_record(self) -> AppliedRecord | None:
        try:
            return AppliedRecord.model_validate_json(
                (self.applied_dir / RECORD_FNAME).read_text()
            )
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.warning(f"the applied set's record is unreadable: {e!r}")
            return None

    def history(self, event: str, **fields: Any) -> None:
        """Append one line of evidence. Never raises: the history is a record of
        what happened, not a condition for it."""
        _line = json.dumps({"time": _now(), "event": event, **fields}, sort_keys=True)
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
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

    def _put_in_place(self, record: AppliedRecord | None, slots) -> bool:
        self._write_fstab(record)
        self._mount(record)
        return self._run_applies(slots)

    def switch(
        self, *, new: AppliedRecord | None, base_slots: list[ParameterSlot]
    ) -> bool:
        """Put the set in staging (or none, for an empty set) in place of the applied
        one, and run the `apply` of every slot whose contents change.

        On failure the applied set is put back, its `apply`s run again, and
        ParameterSetFailed raised. Returns True when a slot's `apply` is `reboot`.
        """
        _old = self.applied_record()
        _touched = {
            _s.name: _s
            for _s in [*(_old.slots if _old else []), *(new.slots if new else [])]
        }.values()
        _previous = self.root / PREVIOUS
        shutil.rmtree(_previous, ignore_errors=True)
        try:
            self._unmount(base_slots)
            if self.applied_dir.exists():
                os.replace(self.applied_dir, _previous)
            if new is not None:
                (self.staging_dir / RECORD_FNAME).write_text(new.model_dump_json())
                os.replace(self.staging_dir, self.applied_dir)
            _reboot = self._put_in_place(new, _touched)
        except Exception as e:
            logger.error(
                f"failed to put the parameter set in place: {e!r}; rolling back"
            )
            self._roll_back(_old, base_slots, _touched, e)
            raise ParameterSetFailed(f"the set was not put in place: {e!r}") from e
        shutil.rmtree(_previous, ignore_errors=True)
        if new is not None:
            self.history("applied", **new.evidence())
        return _reboot

    def _roll_back(self, old, base_slots, touched, cause: Exception) -> None:
        _previous = self.root / PREVIOUS
        try:
            self._unmount(base_slots)
            if _previous.exists():
                shutil.rmtree(self.applied_dir, ignore_errors=True)
                os.replace(_previous, self.applied_dir)
            elif old is None:
                shutil.rmtree(self.applied_dir, ignore_errors=True)
            self._put_in_place(old, touched)
            self.history(
                "rolled_back",
                cause=repr(cause),
                set=old.set_manifest_digest if old else None,
            )
        except Exception as e:
            logger.error(f"failed to roll the parameter set back: {e!r}")
            self.history("roll_back_failed", cause=repr(cause), error=repr(e))

    # ------ the applied set, verified ------ #

    def check_tree(self, record: AppliedRecord) -> str | None:
        """Why the unpacked files are not the set's, or None. Against the config's
        file list: every file there, with its digest, and nothing else."""
        try:
            _config = ParameterSetConfig.parse_metafile(
                (self.applied_dir / CONFIG_FNAME).read_text()
            )
        except Exception as e:
            return f"the set's config is unreadable: {e!r}"
        _tree = self.applied_dir / TREE_DNAME
        _expected = {_f.path: _f for _f in _config.files}
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
        if _missing := set(_expected) - _found:
            return f"{len(_missing)} files of the set are missing"
        return None

    def check_at_startup(
        self,
        base: RunningBase | None,
        *,
        restore_base_version: Callable[[AppliedRecord], None],
    ) -> AppliedRecord | None:
        """At otaclient's start: the applied set, if it still holds; else it is
        dropped, the slots get the base's defaults and the slot reports the base.

        The fstab entries mounted the set at boot, before anything read it; this is
        where it is checked, and where that is recorded.
        """
        _record = self.applied_record()
        if _record is None:
            return None
        _why = None
        if base is None or str(base.manifest_digest) != _record.base_manifest_digest:
            _why = "the set was not applied on the running base"
        else:
            _why = self.check_tree(_record)
        if _why is None:
            _unmounted = [
                _s for _s in _record.slots if not self._mounter.is_mounted(_s.path)
            ]
            try:
                # fstab's entries are `nofail`: a slot that did not mount at boot ran
                # with the base's defaults until now, so its `apply` runs again
                for _slot in _unmounted:
                    self._mounter.bind_ro(
                        self.applied_dir / TREE_DNAME / _slot.relpath, _slot.path
                    )
                if _unmounted:
                    self._run_applies(_unmounted)
                    self.history(
                        "remounted",
                        set=_record.set_manifest_digest,
                        slots=[_s.name for _s in _unmounted],
                    )
            except Exception as e:
                _why = f"the set could not be mounted: {e!r}"
        if _why is None:
            self.history("running", **_record.evidence())
            return _record

        logger.error(f"the applied parameter set does not hold ({_why}): dropping it")
        try:
            self._unmount(_record.slots)
            self._write_fstab(None)
            shutil.rmtree(self.applied_dir, ignore_errors=True)
            self._run_applies(_record.slots)
            restore_base_version(_record)
        except Exception as e:
            logger.error(f"failed to drop the parameter set: {e!r}")
        self.history("dropped", reason=_why, **_record.evidence())
        return None

    def carry_history_to(self, standby_root: Path, *, reason: str) -> None:
        """Copy the history into the slot a full OTA wrote, saying that the applied
        set stays behind. Never raises."""
        try:
            _record = self.applied_record()
            if _record is None and not self.history_file.exists():
                return  # no set was ever applied here: nothing to carry
            _dst = standby_root / Path(cfg.PARAMETER_SET_DPATH).relative_to("/")
            _dst.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.history_file.exists():
                shutil.copyfile(self.history_file, _dst / HISTORY_FNAME)
            ParameterSetStore(root=_dst, fstab=self.fstab).history(
                "left_behind",
                reason=reason,
                set=_record.set_manifest_digest if _record else None,
                version=_record.version if _record else None,
            )
        except Exception as e:
            logger.warning(f"failed to carry the parameter set history: {e!r}")


def _digest_of(path: Path) -> Sha256Digest:
    _h = sha256()
    with open(path, "rb") as _f:
        while _chunk := _f.read(1024**2):
            _h.update(_chunk)
    return Sha256Digest(_h.hexdigest())
