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
"""Driving the DPI, the device side of a partition-based OTA image.

A partition-based payload is written to the standby slot as bytes, not rebuilt from
files, and what may be written where is decided by the partition layout contract
rather than by otaclient. The implementation of that contract is the DPI, a
dependency-free bundle installed in the image, and otaclient drives it here instead
of reimplementing it: the rules about which slot is standby, how a delta is applied,
when a trial boot may be committed and what makes a pair of blobs bootable all stay
in one place, and an image can carry a newer DPI than the otaclient that calls it.

The protocol is three words on stdout — `PROGRESS <0..100>`, `VERSION <version>` and
`REBOOT` — and everything else, on either stream, is a log line. Failure is a
non-zero exit; a traceback never reaches us.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Union

from otaclient_common import _env as otaclient_env

logger = logging.getLogger(__name__)

DEFAULT_DPI_PATH = "/usr/local/sbin/rootfs-ua-dpi"
"""The privileged wrapper the image install writes: runs the bundle as root and accepts
only the DPI's own verbs."""

DEFAULT_TIMEOUT = 60
"""Enough for anything that only reads state. Writing a partition gets its own."""

INSTALL_TIMEOUT = 3600
"""A slot write is gigabytes onto storage that may be slow; the DPI reports progress
throughout, so a stall is visible long before this."""


class DPIError(Exception):
    """The DPI refused, failed, or could not be run at all."""


@dataclass
class DPIResult:
    """What one DPI run reported."""

    returncode: int
    version: Optional[str] = None
    reboot_required: bool = False
    output: str = ""
    """Stdout that is not protocol, kept only for the verbs that answer with data.
    For every other verb those lines are log lines and go to the log."""
    last_error: str = ""
    """The last line the DPI wrote to stderr, which is where it says why. Carried so
    that a failure can be reported with its reason instead of an exit code."""


@dataclass
class SlotLayout:
    """The slots as the DPI resolved them from the running root.

    Device paths are the DPI's answer, not otaclient's guess: on this layout a slot
    is a PARTLABEL behind a device-mapper target, so the mapping cannot be derived
    from the root device alone.
    """

    active_slot: str
    standby_slot: str
    active_dev: str
    standby_dev: str
    platform: str = "grub"
    """Which device side the DPI found itself on. It decides where state that must
    outlive the reboot can be kept, which differs per platform and is not derivable
    from the slots."""

    @classmethod
    def from_json(cls, raw: str) -> SlotLayout:
        try:
            _parsed = json.loads(raw)
            return cls(
                active_slot=_parsed["active_slot"],
                standby_slot=_parsed["standby_slot"],
                active_dev=_parsed["active_dev"],
                standby_dev=_parsed["standby_dev"],
                platform=_parsed.get("platform") or "grub",
            )
        except (ValueError, KeyError, TypeError) as e:
            raise DPIError(f"the DPI reported a layout we cannot read: {e!r}") from e


class DPIClient:
    """One process per call, which is also how the eSync shim drives it.

    The DPI is coarse by design — `get-version`, `install` (minutes to hours),
    `resume` — so process startup is not worth avoiding, and a crash cannot take
    otaclient with it.
    """

    def __init__(self, executable: Union[str, Path] = DEFAULT_DPI_PATH) -> None:
        self.executable = str(executable)

    # ------ running it ------ #

    def _run(
        self,
        args: List[str],
        *,
        timeout: int = DEFAULT_TIMEOUT,
        on_progress: Optional[Callable[[int], None]] = None,
        check: bool = True,
        capture: bool = False,
        ecu_id: Optional[str] = None,
        release_key: Optional[str] = None,
    ) -> DPIResult:
        """Run one verb, translating its stdout as it arrives.

        Progress is forwarded while it is still being made, which is the only reason
        this reads the stream instead of waiting for the process: a slot write is the
        longest phase of the update and otaclient has to keep reporting during it.
        """
        _cmd = [str(self.executable)] + args
        if _chroot := otaclient_env.get_dynamic_client_chroot_path():
            # The DPI belongs to the image on the slot, and a dynamically loaded
            # otaclient runs with the client's own app image as its root — which does
            # not carry the DPI, only the client. The running slot is rbound at
            # /host_root, so the DPI is run in the root it came from: its paths, its
            # python, its bundle. Without this the boot controller cannot even ask
            # which slot it is on (seen on the reference VM).
            _cmd = ["chroot", _chroot] + _cmd
        logger.debug(f"calling the DPI: {_cmd}")

        _env = os.environ.copy()
        # Which payload of a multi-ECU image is this device's: the DPI refuses to
        # choose, and otaclient knows from ecu_info.yaml.
        if ecu_id:
            _env["ROOTFS_UA_ECU_ID"] = ecu_id
        if release_key:
            _env["ROOTFS_UA_RELEASE_KEY"] = release_key

        try:
            _proc = subprocess.Popen(
                _cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                env=_env,
            )
        except OSError as e:
            raise DPIError(f"cannot run the DPI at {self.executable}: {e!r}") from e

        _result = DPIResult(returncode=-1)
        _captured: List[str] = []
        _timed_out = threading.Event()

        def _kill() -> None:
            _timed_out.set()
            _proc.kill()

        # stderr is the DPI's log. It is drained on its own thread, because a pipe
        # left unread for the length of a slot write fills and blocks the DPI -- and
        # it is never merged into stdout, which is the protocol and, for `layout` and
        # `source-digest`, JSON (merging them broke both, seen on the reference VM).
        # The timer bounds the read loop, which `wait(timeout)` after EOF would not.
        def _drain_stderr() -> None:
            assert _proc.stderr is not None
            for _line in _proc.stderr:
                _line = _line.strip()
                if _line:
                    logger.info(f"dpi: {_line}")
                    _result.last_error = _line

        _stderr_reader = threading.Thread(target=_drain_stderr, daemon=True)
        _stderr_reader.start()
        _timer = threading.Timer(timeout, _kill)
        _timer.start()
        try:
            assert _proc.stdout is not None
            for _line in _proc.stdout:
                _line = _line.strip()
                if not _line:
                    continue
                if _line.startswith("PROGRESS "):
                    if on_progress is not None:
                        try:
                            on_progress(int(_line[len("PROGRESS ") :]))
                        except ValueError:
                            logger.warning(f"unparsable progress from the DPI: {_line}")
                elif _line.startswith("VERSION "):
                    _result.version = _line[len("VERSION ") :].strip()
                elif _line == "REBOOT":
                    _result.reboot_required = True
                elif capture:
                    _captured.append(_line)
                else:
                    # Not protocol and not asked for: logged, like stderr.
                    logger.info(f"dpi: {_line}")
            _result.returncode = _proc.wait()
        finally:
            _timer.cancel()
            _stderr_reader.join(timeout=5)
            for _stream in (_proc.stdout, _proc.stderr):
                if _stream is not None:
                    _stream.close()
        if _timed_out.is_set():
            raise DPIError(f"the DPI did not finish {args[0]} within {timeout}s")

        _result.output = "\n".join(_captured)
        if check and _result.returncode != 0:
            raise DPIError(
                f"the DPI failed {args[0]} with exit {_result.returncode}; "
                "the reason is in the lines above"
            )
        return _result

    # ------ the verbs ------ #

    def get_version(self, *, name: Optional[str] = None) -> str:
        """The version of the running image, or of the component `name`."""
        _args = ["get-version"]
        if name is not None:
            _args += ["--name", name]
        _res = self._run(_args)
        if _res.version is None:
            raise DPIError("the DPI answered get-version without a version")
        return _res.version

    def layout(self) -> SlotLayout:
        """Which slot is active, which is standby, and the devices behind them."""
        return SlotLayout.from_json(
            self._run(["layout", "--json"], capture=True).output
        )

    def source_digest(self, *, size: int) -> str:
        """The hex sha256 of the first `size` bytes of the committed slot.

        What a delta says it applies to is named by digest, never by version, so this
        is the only thing that tells us whether a delta fits this device — and it has
        to be known before downloading, which is the whole reason to ask.
        """
        _res = self._run(["source-digest", "--size", str(size)], capture=True)
        try:
            return str(json.loads(_res.output)["digest"])
        except (ValueError, KeyError, TypeError) as e:
            raise DPIError(f"the DPI reported a digest we cannot read: {e!r}") from e

    def install(
        self,
        *,
        package: Union[str, Path],
        version: str,
        name: str,
        rollback: bool = False,
        on_progress: Optional[Callable[[int], None]] = None,
        timeout: int = INSTALL_TIMEOUT,
        ecu_id: Optional[str] = None,
        release_key: Optional[str] = None,
    ) -> bool:
        """Write the payload onto the standby slot.

        Returns:
            Whether the update takes effect only after a reboot, which on this layout
            it always does for a rootfs — but the DPI says so rather than the caller
            assuming it, because a payload that needs no reboot is a legal payload.
        """
        _args = [
            "install",
            "--package",
            str(package),
            "--version",
            version,
            "--name",
            name,
        ]
        if rollback:
            _args.append("--rollback")
        return self._run(
            _args,
            timeout=timeout,
            on_progress=on_progress,
            ecu_id=ecu_id,
            release_key=release_key,
        ).reboot_required

    def resume(self) -> Optional[str]:
        """Conclude an update staged before this boot.

        The DPI decides commit or rollback from the health check; a non-zero exit is
        its verdict that this boot failed, not a bug in otaclient, so the caller
        turns it into a failed switch rather than an error.
        """
        _res = self._run(["resume"], check=False)
        if _res.returncode != 0:
            raise DPIError(
                f"the DPI could not conclude the update: exit {_res.returncode}"
                + (f": {_res.last_error}" if _res.last_error else "")
            )
        return _res.version
