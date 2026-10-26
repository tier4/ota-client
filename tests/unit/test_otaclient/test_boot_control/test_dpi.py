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
"""The protocol otaclient reads off the DPI's stdout.

The DPI is a separate program installed in the image, so these tests run a stand-in
that prints what the real one prints. What is being checked is the contract between
the two -- three words on stdout, logs everywhere else, failure as an exit code --
because a drift there is invisible until an update runs on a device.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from otaclient.boot_control import _dpi
from otaclient.boot_control._dpi import (
    DEFAULT_TIMEOUT,
    SOURCE_DIGEST_MIN_RATE,
    TERMINATE_GRACE,
    DPIClient,
    DPIError,
    SlotLayout,
)
from otaclient.boot_control.protocol import (
    PartitionInstallerProtocol,
    PartitionInstallError,
)

A_LAYOUT = {
    "platform": "grub",
    "active_slot": "rootfs_a",
    "standby_slot": "rootfs_b",
    "active_dev": "/dev/sda3",
    "standby_dev": "/dev/sda4",
}


def fake_dpi(tmp_path: Path, body: str) -> Path:
    """A stand-in DPI: `body` is python, with `args` bound to the verb and its flags."""
    _script = tmp_path / "dpi-wrapper"
    _script.write_text(
        "#!{interpreter}\nimport sys\nargs = sys.argv[1:]\n{body}\n".format(
            interpreter=sys.executable, body=body
        )
    )
    _script.chmod(_script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return _script


@pytest.fixture
def our_log(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """The DPI's lines are INFO in our log; caplog only sees them when asked."""
    caplog.set_level(logging.DEBUG, logger="otaclient.boot_control")
    return caplog


def test_the_client_is_a_partition_installer():
    """What the partition-image updater asks of its installer, the DPI answers; a
    refusal is the error that updater catches."""
    assert isinstance(
        DPIClient("/usr/local/sbin/rootfs-ua-dpi"), PartitionInstallerProtocol
    )
    assert issubclass(DPIError, PartitionInstallError)


class TestGetVersion:
    def test_reads_the_version_line(self, tmp_path: Path):
        _dpi = fake_dpi(tmp_path, "print('VERSION 2.9.0')")
        assert DPIClient(_dpi).get_version() == "2.9.0"

    def test_passes_the_component_name(self, tmp_path: Path):
        _dpi = fake_dpi(
            tmp_path,
            "print('VERSION ' + (args[args.index('--name') + 1] if '--name' in args else 'no-name'))",
        )
        assert DPIClient(_dpi).get_version(name="AGENT") == "AGENT"

    def test_an_answer_without_a_version_is_an_error(self, tmp_path: Path):
        """Exit 0 and no VERSION line means the DPI answered something we cannot use;
        reporting an empty version to the server would look like a successful read."""
        _dpi = fake_dpi(tmp_path, "print('nothing to say')")
        with pytest.raises(DPIError):
            DPIClient(_dpi).get_version()

    def test_a_missing_dpi_is_an_error_not_a_crash(self, tmp_path: Path):
        with pytest.raises(DPIError, match="cannot run the DPI"):
            DPIClient(tmp_path / "not-installed").get_version()


class TestInstall:
    def test_forwards_the_package_and_the_version_and_names_the_ecu(
        self, tmp_path: Path
    ):
        """An image built for a vehicle carries one payload per ECU, and the DPI
        refuses to choose between them: otaclient says which one through the
        environment the DPI reads."""
        _seen = tmp_path / "seen"
        _dpi = fake_dpi(
            tmp_path,
            "import os\n"
            f"open({str(_seen)!r}, 'w').write(' '.join(args) + '|' + "
            "os.environ.get('ROOTFS_UA_ECU_ID', '-') + ' ' + os.environ.get('ROOTFS_UA_RELEASE_KEY', '-'))\n"
            "print('REBOOT')",
        )
        DPIClient(_dpi).install(
            package=Path("/opt/data/otaclient/staged/3_0_0/image"),
            version="2.9.0",
            ecu_id="perception",
            release_key="prd",
        )
        _argv, _env = _seen.read_text().split("|")
        assert _argv.split() == [
            "install",
            "--package",
            "/opt/data/otaclient/staged/3_0_0/image",
            "--version",
            "2.9.0",
        ], "no component name, no rollback: otaclient has neither to give"
        assert _env == "perception prd"

    def test_nothing_of_otaclients_environment_reaches_the_dpi(
        self, tmp_path: Path, monkeypatch
    ):
        """The DPI takes its trust anchor and its paths from ROOTFS_UA_* variables:
        whatever otaclient inherited must not become such an input. The two
        selectors are the only variables it is given, beside a fixed PATH."""
        monkeypatch.setenv("ROOTFS_UA_IMAGE_CA", "/tmp/not-the-real-one.pem")
        monkeypatch.setenv("LD_PRELOAD", "/tmp/evil.so")
        _seen = tmp_path / "env"
        _dpi = fake_dpi(
            tmp_path,
            "import os\n"
            f"open({str(_seen)!r}, 'w').write(' '.join(sorted(os.environ)))\n"
            "print('REBOOT')",
        )
        DPIClient(_dpi).install(
            package=Path("p"), version="1", ecu_id="autoware", release_key="dev"
        )
        assert _seen.read_text().split() == [
            "LANG",
            "PATH",
            "ROOTFS_UA_ECU_ID",
            "ROOTFS_UA_RELEASE_KEY",
        ]

    def test_progress_is_reported_as_it_arrives(self, tmp_path: Path):
        """A slot write is the longest phase of an update; otaclient has to keep
        reporting during it rather than after. The stand-in waits, after each line,
        for the callback to have seen it before it writes the next: a reader that
        collected everything at exit would never let it finish."""
        _ack = tmp_path / "ack"
        _dpi = fake_dpi(
            tmp_path,
            "import os, sys, time\n"
            "for p in (0, 50, 100):\n"
            "    print('PROGRESS %d' % p); sys.stdout.flush()\n"
            "    for _ in range(200):\n"
            f"        if os.path.exists({str(_ack)!r} + str(p)): break\n"
            "        time.sleep(0.05)\n"
            "    else: sys.exit(3)\n"
            "print('REBOOT')",
        )
        _seen = []

        def _on_progress(p: int) -> None:
            _seen.append(p)
            (tmp_path / f"ack{p}").touch()

        DPIClient(_dpi).install(
            package=Path("p"),
            version="1",
            ecu_id="autoware",
            release_key="dev",
            on_progress=_on_progress,
        )
        assert _seen == [0, 50, 100]

    def test_unparsable_progress_is_logged_not_fatal(self, tmp_path: Path, our_log):
        _dpi = fake_dpi(tmp_path, "print('PROGRESS lots')\nprint('REBOOT')")
        DPIClient(_dpi).install(
            package=Path("p"),
            version="1",
            ecu_id="autoware",
            release_key="dev",
            on_progress=lambda _: None,
        )
        assert "unparsable progress" in our_log.text

    def test_a_dpi_that_asks_for_no_reboot_is_logged_not_refused(
        self, tmp_path: Path, our_log
    ):
        """The update is concluded by the trial boot otaclient reboots into whatever
        the DPI said; the oddity is worth a line in the log."""
        _dpi = fake_dpi(tmp_path, "print('PROGRESS 100')")
        DPIClient(_dpi).install(
            package=Path("p"), version="1", ecu_id="autoware", release_key="dev"
        )
        assert "without asking for a reboot" in our_log.text

    def test_a_refused_install_raises_with_the_reason(self, tmp_path: Path):
        _dpi = fake_dpi(
            tmp_path,
            "print('the delta applies to a9ec... which is not what /dev/sda3 holds', file=sys.stderr)\n"
            "sys.exit(1)",
        )
        with pytest.raises(DPIError, match="exit 1: the delta applies"):
            DPIClient(_dpi).install(
                package=Path("p"), version="1", ecu_id="autoware", release_key="dev"
            )


class TestResume:
    def test_resume_returns_the_committed_version(self, tmp_path: Path):
        _dpi = fake_dpi(tmp_path, "print('VERSION 2.9.0')")
        assert DPIClient(_dpi).resume() == "2.9.0"

    def test_a_failed_resume_raises(self, tmp_path: Path):
        """The DPI's verdict that this boot did not come up healthy is not otaclient's
        bug to hide: the caller turns it into a failed switch."""
        _dpi = fake_dpi(
            tmp_path,
            "print('staged 1.7.2 on rootfs_a but booted rootfs_b; the update did not take: failed: fhs-fail.service', file=sys.stderr)\n"
            "sys.exit(1)",
        )
        with pytest.raises(DPIError, match="did not take"):
            DPIClient(_dpi).resume()

    def test_a_failed_resume_reports_the_dpis_own_sentence(self, tmp_path: Path):
        """What FMS shows is the DPI's ERROR line alone: its log prefix and the INFO
        lines around it are log, not reason."""
        _dpi = fake_dpi(
            tmp_path,
            "print('2026-10-26 09:28:48,587 INFO dpi.state: cleared the pending update record', file=sys.stderr)\n"
            "print('2026-10-26 09:28:48,590 ERROR dpi: staged 1.7.3 on rootfs_b but booted rootfs_a; the update did not take: failed: fhs-slow-start.service', file=sys.stderr)\n"
            "print('2026-10-26 09:28:48,591 INFO dpi.data_image: fhs-test: next dropped', file=sys.stderr)\n"
            "sys.exit(1)",
        )
        with pytest.raises(DPIError) as _exc:
            DPIClient(_dpi).resume()
        assert str(_exc.value) == (
            "staged 1.7.3 on rootfs_b but booted rootfs_a; "
            "the update did not take: failed: fhs-slow-start.service"
        )

    def test_a_failed_resume_without_a_word_names_the_exit(self, tmp_path: Path):
        _dpi = fake_dpi(tmp_path, "sys.exit(2)")
        with pytest.raises(DPIError, match=r"could not conclude the update \(exit 2\)"):
            DPIClient(_dpi).resume()

    def test_resume_is_given_the_health_checks_time(self, tmp_path: Path, monkeypatch):
        """On the trial boot the DPI's resume waits for the health verdict (minutes)
        before it commits; cut short, a healthy slot would stay uncommitted."""
        _seen = {}

        def _fake_run(self, args, **kwargs):
            _seen["timeout"] = kwargs.get("timeout")
            return _dpi.DPIResult(returncode=0, version="1")

        monkeypatch.setattr(DPIClient, "_run", _fake_run)
        DPIClient("dpi").resume()
        assert _seen["timeout"] == _dpi.RESUME_TIMEOUT >= 2 * 900


class TestLayout:
    def test_reads_the_slots_the_dpi_resolved(self, tmp_path: Path):
        _dpi = fake_dpi(tmp_path, f"print({json.dumps(json.dumps(A_LAYOUT))})")
        assert DPIClient(_dpi).layout() == SlotLayout(
            active_slot="rootfs_a",
            standby_slot="rootfs_b",
            active_dev="/dev/sda3",
            standby_dev="/dev/sda4",
            platform="grub",
        )

    def test_the_dpis_log_on_stderr_does_not_reach_the_answer(self, tmp_path: Path):
        """The DPI logs what it resolved on stderr before it prints the JSON. Merging
        the two streams made every layout unreadable."""
        _layout = {**A_LAYOUT, "boot_dev": "/dev/sda2"}
        _dpi = fake_dpi(
            tmp_path,
            "print('INFO dpi.platforms.grub.layout: root is on /dev/sda3', file=sys.stderr)\n"
            f"print({json.dumps(json.dumps(_layout))})",
        )
        assert DPIClient(_dpi).layout().standby_slot == "rootfs_b"

    def test_source_digest_may_ask_about_a_data_image(self, tmp_path: Path):
        _dpi = fake_dpi(
            tmp_path,
            "import json; print(json.dumps({'digest': 'f' * 64 if '--data-image' in args else 'a' * 64}))",
        )
        assert DPIClient(_dpi).source_digest(size=4096) == "a" * 64
        assert (
            DPIClient(_dpi).source_digest(size=4096, data_image="ml_package")
            == "f" * 64
        )

    def test_source_digest_waits_in_proportion_to_what_is_hashed(self, monkeypatch):
        """The DPI reads the committed slot up to the delta's source size -- gigabytes
        on eMMC -- so a fixed read timeout would cut a fitting delta off as 'not
        fitting'."""
        _seen = []

        def _fake_run(self, args, **kwargs):
            _seen.append(kwargs.get("timeout"))
            return _dpi.DPIResult(returncode=0, output='{"digest": "ab"}')

        monkeypatch.setattr(DPIClient, "_run", _fake_run)
        DPIClient("dpi").source_digest(size=4096)
        DPIClient("dpi").source_digest(size=64 << 30)
        assert _seen == [DEFAULT_TIMEOUT, (64 << 30) // SOURCE_DIGEST_MIN_RATE]
        assert _seen[1] > DEFAULT_TIMEOUT

    @pytest.mark.parametrize(
        "answer",
        [
            "not json at all",
            "[]",
            '{"active_slot": "rootfs_a"}',
            json.dumps({k: v for k, v in A_LAYOUT.items() if k != "platform"}),
        ],
        ids=["text", "a list", "half a layout", "no platform"],
    )
    def test_an_unreadable_layout_is_an_error(self, tmp_path: Path, answer: str):
        """Never guess a slot, nor the platform: writing the wrong partition is
        unrecoverable, and the platform decides where the state goes."""
        _dpi = fake_dpi(tmp_path, f"print({answer!r})")
        with pytest.raises(DPIError, match="layout"):
            DPIClient(_dpi).layout()


class TestRunning:
    def test_a_dpi_that_overruns_its_time_is_ended_and_reported(self, tmp_path: Path):
        _dpi = fake_dpi(
            tmp_path,
            "import time; print('PROGRESS 0'); sys.stdout.flush(); time.sleep(60)",
        )
        _started = time.monotonic()
        with pytest.raises(DPIError, match=r"did not finish resume within 1s"):
            DPIClient(_dpi)._run(["resume"], timeout=1)
        assert time.monotonic() - _started < TERMINATE_GRACE

    def test_a_dpi_that_ignores_sigterm_is_killed(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(_dpi, "TERMINATE_GRACE", 1)
        _dpi_script = fake_dpi(
            tmp_path,
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('PROGRESS 0'); sys.stdout.flush(); time.sleep(60)",
        )
        _started = time.monotonic()
        with pytest.raises(DPIError, match="did not finish"):
            DPIClient(_dpi_script)._run(["install"], timeout=1)
        assert time.monotonic() - _started < 10

    def test_a_callback_that_raises_does_not_leave_the_dpi_running(
        self, tmp_path: Path
    ):
        """Whatever interrupts the reading, the DPI is neither left writing on its
        own nor left a zombie."""
        _pid_file = tmp_path / "pid"
        _dpi = fake_dpi(
            tmp_path,
            "import os, time\n"
            f"open({str(_pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "print('PROGRESS 10'); sys.stdout.flush(); time.sleep(60)",
        )

        def _boom(_: int) -> None:
            raise RuntimeError("the reporter is broken")

        with pytest.raises(RuntimeError, match="the reporter is broken"):
            DPIClient(_dpi)._run(["install"], on_progress=_boom)
        _pid = int(_pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(_pid, 0)  # reaped, not a zombie and not alive

    def test_the_dpis_log_keeps_its_level_in_ours(self, tmp_path: Path, our_log):
        """An ERROR of the DPI is an ERROR in the uploaded log, not an INFO line
        that happens to contain the word."""
        _dpi = fake_dpi(
            tmp_path,
            "print('2026-10-26 09:28:48,587 INFO dpi.state: cleared the pending update record', file=sys.stderr)\n"
            "print('2026-10-26 09:28:48,590 ERROR dpi.health: the health check did not pass', file=sys.stderr)\n"
            "print('sudo: a word from the wrapper', file=sys.stderr)\n"
            "print('VERSION 1')",
        )
        DPIClient(_dpi).get_version()
        _records = {
            r.getMessage(): r.levelno
            for r in our_log.records
            if r.getMessage().startswith("dpi")
        }
        assert _records["dpi dpi.state: cleared the pending update record"] == (
            logging.INFO
        )
        assert _records["dpi dpi.health: the health check did not pass"] == (
            logging.ERROR
        )
        assert _records["dpi: sudo: a word from the wrapper"] == logging.INFO


def test_log_lines_do_not_become_protocol(tmp_path: Path, our_log):
    """Everything that is not one of the three words is a log line, including a line
    that merely mentions them."""
    _dpi = fake_dpi(
        tmp_path,
        "print('about to PROGRESS through the write')\nprint('VERSION 2.9.0')",
    )
    assert DPIClient(_dpi).get_version() == "2.9.0"
    assert "about to PROGRESS through the write" in our_log.text


def test_a_dynamically_loaded_client_runs_the_dpi_in_the_slots_own_root(
    tmp_path, monkeypatch
):
    """A dynamic otaclient runs with its own app image as root, and that image carries
    the client and nothing else. The DPI is the platform's, on the slot, rbound at
    /host_root -- so it is run there. Without this, the boot controller could not
    even ask which slot it was on."""
    _calls = []

    def _fake_popen(cmd, **kwargs):
        _calls.append(cmd)
        raise OSError(2, "no such file")  # far enough: the command line is the point

    monkeypatch.setattr(_dpi.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(
        _dpi.otaclient_env, "get_dynamic_client_chroot_path", lambda: "/host_root"
    )

    with pytest.raises(DPIError):
        DPIClient(executable="/usr/local/sbin/dpi-wrapper")._run(["get-version"])

    assert _calls == [
        ["chroot", "/host_root", "/usr/local/sbin/dpi-wrapper", "get-version"]
    ]


def test_the_dpi_is_run_directly_when_the_client_is_the_images_own(
    tmp_path, monkeypatch
):
    _calls = []

    def _fake_popen(cmd, **kwargs):
        _calls.append(cmd)
        raise OSError(2, "no such file")

    monkeypatch.setattr(_dpi.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(
        _dpi.otaclient_env, "get_dynamic_client_chroot_path", lambda: None
    )

    with pytest.raises(DPIError):
        DPIClient(executable="/usr/local/sbin/dpi-wrapper")._run(["get-version"])

    assert _calls == [["/usr/local/sbin/dpi-wrapper", "get-version"]]


class _ECUInfo:
    def __init__(self, dpi_executable):
        self.bootloader = "grub-verity"
        self.dpi_executable = dpi_executable


def test_the_dpi_is_the_executable_ecu_info_names(monkeypatch):
    """otaclient drives whatever DPI the image build installed and wrote into
    ecu_info.yaml: it names no agent, no wrapper, no path of its own. A DPI layout
    whose ecu_info.yaml names none is a misconfigured image, refused up front."""
    from otaclient.configs import cfg as cfg_module

    monkeypatch.setattr(cfg_module, "ecu_info", _ECUInfo("/usr/local/sbin/dpi-wrapper"))
    assert _dpi.dpi_from_ecu_info().executable == "/usr/local/sbin/dpi-wrapper"

    monkeypatch.setattr(cfg_module, "ecu_info", _ECUInfo(None))
    with pytest.raises(DPIError, match="dpi_executable"):
        _dpi.dpi_from_ecu_info()
