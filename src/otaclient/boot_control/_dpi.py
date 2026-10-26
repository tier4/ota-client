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
rather than by otaclient. The implementation of that contract is the DPI, the
platform's own installer (ota-esync-agent's `rootfs-ua-dpi`): a self-contained
bundle the platform tooling puts into the image, not part of otaclient. otaclient
drives it here instead of reimplementing it: the rules about which slot is standby,
how a delta is applied, when a trial boot may be committed and what makes a pair of
blobs bootable all stay in one place, and an image can carry a newer DPI than the
otaclient that calls it. `DPIClient` is the `PartitionInstallerProtocol` of the
partition-image updater.

The protocol is three words on stdout -- `PROGRESS <0..100>`, `VERSION <version>` and
`REBOOT` -- and everything else, on either stream, is a log line. Failure is a
non-zero exit; a traceback never reaches us.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from otaclient.boot_control.protocol import (
    PartitionInstallerProtocol,
    PartitionInstallError,
)
from otaclient_common import _env as otaclient_env

logger = logging.getLogger(__name__)


# the DPI logs to stderr as "<date> <time> <LEVEL> <logger>: <message>"; the message
# alone is what a failure reason is built from, and the level is carried into our log
_LOG_LINE_PREFIX = re.compile(
    r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:,\d{3})? (?P<level>[A-Z]+) (?P<name>\S+): "
)

DEFAULT_TIMEOUT = 60
"""Enough for a verb that only reads state. The verbs that wait or write get their
own, below."""

INSTALL_TIMEOUT = 3600
"""A slot write is gigabytes onto storage that may be slow; the DPI reports progress
throughout, so a stall is visible long before this."""

RESUME_TIMEOUT = 1800
"""`resume` on the trial boot blocks until the health check has its verdict: the DPI
waits for that with its own backstop (`health_timeout`, 900 s unless the image sets
`ROOTFS_UA_HEALTH_TIMEOUT`), then commits. Cutting it short would leave a healthy
slot uncommitted, so this is twice the DPI's backstop, not a read timeout."""

SOURCE_DIGEST_MIN_RATE = 32 << 20
"""`source-digest` reads and hashes the committed slot up to the delta's source size,
gigabytes on eMMC or SATA. Its timeout grows with the size at this assumed rate
(bytes per second), with `DEFAULT_TIMEOUT` as the floor."""

TERMINATE_GRACE = 10
"""Seconds a DPI that overran its timeout gets to end on SIGTERM before SIGKILL."""

_DPI_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
}
"""The environment the DPI is run in: not otaclient's own. The DPI reads
`ROOTFS_UA_*` variables for its trust anchor and its paths, so nothing otaclient
happens to have inherited may reach it; the two selectors below are the only
variables added, and neither names a path."""

_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class DPIError(PartitionInstallError):
    """The DPI refused, failed, or could not be run at all."""


def _split_log_line(line: str) -> tuple[str | None, str | None, str]:
    """A line of the DPI's log as (level, logger name, message).

    A line without the DPI's log prefix (the wrapper's, a traceback's continuation)
    is a message with no level and no name.
    """
    _prefix = _LOG_LINE_PREFIX.match(line)
    if _prefix is None:
        return None, None, line
    return _prefix.group("level"), _prefix.group("name"), line[_prefix.end() :]


@dataclass
class DPIResult:
    """What one DPI run reported."""

    returncode: int
    version: str | None = None
    reboot_required: bool = False
    output: str = ""
    """Stdout that is not protocol, kept only for the verbs that answer with data.
    For every other verb those lines are log lines and go to the log."""
    last_error: str = ""
    """The last ERROR line of the DPI's log (or its last unformatted line)."""
    last_line: str = ""
    """The last line of the DPI's log, whatever its level."""

    @property
    def reason(self) -> str:
        """What to tell about a failed verb: its last error, else its last word."""
        return self.last_error or self.last_line


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
    platform: str
    """Which device side the DPI found itself on. It decides where state that must
    outlive the reboot can be kept, which differs per platform and is not derivable
    from the slots. Every DPI that knows more than one platform names it; one that
    does not is refused rather than guessed."""

    @classmethod
    def from_json(cls, raw: str) -> SlotLayout:
        try:
            _parsed = json.loads(raw)
            return cls(
                active_slot=_parsed["active_slot"],
                standby_slot=_parsed["standby_slot"],
                active_dev=_parsed["active_dev"],
                standby_dev=_parsed["standby_dev"],
                platform=_parsed["platform"],
            )
        except (ValueError, KeyError, TypeError) as e:
            raise DPIError(f"the DPI reported a layout we cannot read: {e!r}") from e


class DPIProtocol(PartitionInstallerProtocol, Protocol):
    """What the partition-image boot control asks of the DPI, on top of being the
    installer: the slots, the running version, and the conclusion of a trial."""

    def layout(self) -> SlotLayout: ...

    def get_version(self, *, name: str | None = None) -> str: ...

    def resume(self) -> str | None: ...


def dpi_from_ecu_info() -> DPIClient:
    """The DPI this device's ecu_info.yaml names. The image build that installs the DPI
    writes the path there; a layout driven by the DPI without one is misconfigured."""
    from otaclient.configs.cfg import ecu_info

    if not ecu_info.dpi_executable:
        raise DPIError(
            f"ecu_info.yaml names no dpi_executable, which bootloader {ecu_info.bootloader} needs"
        )
    return DPIClient(ecu_info.dpi_executable)


class DPIClient:
    """One process per call: the DPI's verbs are coarse (`get-version`, `install`,
    `resume`), so process startup is not worth avoiding, and a crash cannot take
    otaclient with it."""

    def __init__(self, executable: str | Path) -> None:
        self.executable = str(executable)

    # ------ running it ------ #

    def _run(
        self,
        args: list[str],
        *,
        timeout: int = DEFAULT_TIMEOUT,
        on_progress: Callable[[int], None] | None = None,
        check: bool = True,
        capture: bool = False,
        ecu_id: str | None = None,
        release_key: str | None = None,
    ) -> DPIResult:
        """Run one verb, translating its stdout as it arrives.

        Progress is forwarded while it is still being made, which is the only reason
        this reads the stream instead of waiting for the process: a slot write is the
        longest phase of the update and otaclient has to keep reporting during it.
        """
        _cmd = [self.executable, *args]
        if _chroot := otaclient_env.get_dynamic_client_chroot_path():
            # NOTE: a dynamically loaded otaclient runs with its own app image as
            #       root, which does not carry the DPI; the DPI is run in the slot's
            #       root, rbound at the chroot path.
            _cmd = ["chroot", _chroot, *_cmd]
        logger.debug(f"calling the DPI: {_cmd}")

        _env = dict(_DPI_ENV)
        # Which payload of a multi-ECU image is this device's: the DPI refuses to
        # choose, and otaclient knows from ecu_info.yaml. Both are matched against
        # the image index's annotations, never used as a path or a command.
        if ecu_id:
            _env["ROOTFS_UA_ECU_ID"] = ecu_id
        if release_key:
            _env["ROOTFS_UA_RELEASE_KEY"] = release_key

        try:
            _proc = subprocess.Popen(
                _cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                env=_env,
            )
        except OSError as e:
            raise DPIError(f"cannot run the DPI at {self.executable}: {e!r}") from e

        _result = DPIResult(returncode=-1)
        _captured: list[str] = []
        _timed_out = threading.Event()

        def _kill() -> None:
            # SIGTERM first: a DPI in the middle of a write or a commit gets to end
            # cleanly; only one that ignores it is killed.
            _timed_out.set()
            _proc.terminate()
            try:
                _proc.wait(TERMINATE_GRACE)
            except subprocess.TimeoutExpired:
                _proc.kill()

        # NOTE: stderr is the DPI's log, drained on its own thread so that it never
        #       fills during a slot write, and never merged into stdout, which is the
        #       protocol (JSON for `layout` and `source-digest`). The timer bounds
        #       the read loop, which `wait(timeout)` after EOF would not.
        def _drain_stderr() -> None:
            assert _proc.stderr is not None
            for _line in _proc.stderr:
                _line = _line.strip()
                if not _line:
                    continue
                _level, _name, _msg = _split_log_line(_line)
                logger.log(
                    _LOG_LEVELS.get(_level or "", logging.INFO),
                    f"dpi {_name}: {_msg}" if _name else f"dpi: {_msg}",
                )
                _result.last_line = _msg
                if _level is None or _level in ("ERROR", "CRITICAL"):
                    _result.last_error = _msg

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
        except BaseException:
            # Whatever interrupted the reading (a progress callback that raised, a
            # signal), the DPI is not left running on its own, nor left a zombie.
            _proc.kill()
            _proc.wait()
            raise
        finally:
            _timer.cancel()
            _stderr_reader.join(timeout=5)
            if _stderr_reader.is_alive():
                logger.warning(
                    "the DPI's stderr is still open after it exited (a child of its "
                    "own?); its log may be incomplete"
                )
            for _stream in (_proc.stdout, _proc.stderr):
                if _stream is not None:
                    _stream.close()
        if _timed_out.is_set() and _result.returncode != 0:
            raise DPIError(f"the DPI did not finish {args[0]} within {timeout}s")

        _result.output = "\n".join(_captured)
        if check and _result.returncode != 0:
            raise DPIError(
                f"the DPI failed {args[0]} with exit {_result.returncode}"
                + (f": {_result.reason}" if _result.reason else "")
            )
        return _result

    # ------ the verbs ------ #

    def get_version(self, *, name: str | None = None) -> str:
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

    # ------ PartitionInstallerProtocol ------ #

    def source_digest(self, *, size: int, data_image: str | None = None) -> str:
        """The hex sha256 of the first `size` bytes of the committed slot, or of the
        file the named data image is mounted from.

        What a delta says it applies to is named by digest, never by version, so this
        is the only thing that tells us whether a delta fits this device -- and it has
        to be known before downloading, which is the whole reason to ask.
        """
        _args = ["source-digest", "--size", str(size)]
        if data_image is not None:
            _args += ["--data-image", data_image]
        _res = self._run(
            _args,
            capture=True,
            timeout=max(DEFAULT_TIMEOUT, size // SOURCE_DIGEST_MIN_RATE),
        )
        try:
            return str(json.loads(_res.output)["digest"])
        except (ValueError, KeyError, TypeError) as e:
            raise DPIError(f"the DPI reported a digest we cannot read: {e!r}") from e

    def install(
        self,
        *,
        package: Path,
        version: str,
        ecu_id: str,
        release_key: str,
        on_progress: Callable[[int], None] | None = None,
        timeout: int = INSTALL_TIMEOUT,
    ) -> None:
        """Write the payload onto the standby slot and arm its trial boot.

        The DPI verifies index.jwt against its own trust anchor and every blob it
        writes against the digest the index names, then arms one trial boot of what
        it wrote. The update is concluded by that boot: a DPI that reports no reboot
        is logged, the trial boot is taken anyway.
        """
        _res = self._run(
            ["install", "--package", str(package), "--version", version],
            timeout=timeout,
            on_progress=on_progress,
            ecu_id=ecu_id,
            release_key=release_key,
        )
        if not _res.reboot_required:
            logger.warning(
                "the DPI applied the payload without asking for a reboot; "
                "the trial boot is taken as for any update"
            )

    def resume(self) -> str | None:
        """Conclude an update staged before this boot.

        On the trial boot this blocks until the health check has its verdict (the
        DPI's own wait, bounded by `RESUME_TIMEOUT`). The DPI decides commit or
        rollback from that verdict; a non-zero exit is its verdict that this boot
        failed, not a bug in otaclient, so the caller turns it into a failed switch
        rather than an error.
        """
        _res = self._run(["resume"], check=False, timeout=RESUME_TIMEOUT)
        if _res.returncode != 0:
            raise DPIError(
                _res.reason
                or f"the DPI could not conclude the update (exit {_res.returncode})"
            )
        return _res.version
