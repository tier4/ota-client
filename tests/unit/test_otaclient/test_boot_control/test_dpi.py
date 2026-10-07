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
the two — three words on stdout, logs everywhere else, failure as an exit code —
because a drift there is invisible until an update runs on a device.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from otaclient.boot_control import _dpi
from otaclient.boot_control._dpi import DPIClient, DPIError, SlotLayout


def fake_dpi(tmp_path: Path, body: str) -> Path:
    """A stand-in DPI: `body` is python, with `args` bound to the verb and its flags."""
    _script = tmp_path / "rootfs-ua-dpi"
    _script.write_text(
        "#!{interpreter}\nimport sys\nargs = sys.argv[1:]\n{body}\n".format(
            interpreter=sys.executable, body=body
        )
    )
    _script.chmod(_script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return _script


class TestGetVersion:
    def test_reads_the_version_line(self, tmp_path: Path):
        _dpi = fake_dpi(tmp_path, "print('VERSION 2.9.0')")
        assert DPIClient(_dpi).get_version() == "2.9.0"

    def test_passes_the_component_name(self, tmp_path: Path):
        _dpi = fake_dpi(
            tmp_path,
            "print('VERSION ' + (args[args.index('--name') + 1] if '--name' in args else 'no-name'))",
        )
        assert DPIClient(_dpi).get_version(name="T4-ROOTFS-AGENT") == "T4-ROOTFS-AGENT"

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
    def test_forwards_every_argument(self, tmp_path: Path):
        _seen = tmp_path / "argv"
        _dpi = fake_dpi(
            tmp_path,
            f"open({str(_seen)!r}, 'w').write(' '.join(args))\nprint('REBOOT')",
        )
        DPIClient(_dpi).install(
            package="/opt/data/ota/image.zip",
            version="2.9.0",
            name="T4-ROOTFS",
            rollback=True,
        )
        _argv = _seen.read_text().split()
        assert _argv[0] == "install"
        assert "--package" in _argv and "/opt/data/ota/image.zip" in _argv
        assert "--version" in _argv and "2.9.0" in _argv
        assert "--name" in _argv and "T4-ROOTFS" in _argv
        assert "--rollback" in _argv

    def test_progress_is_reported_as_it_arrives(self, tmp_path: Path):
        """A slot write is the longest phase of an update; otaclient has to keep
        reporting during it rather than after."""
        _dpi = fake_dpi(
            tmp_path,
            "import sys\n"
            "for p in (0, 50, 100):\n"
            "    print('PROGRESS %d' % p); sys.stdout.flush()\n"
            "print('REBOOT')",
        )
        _seen = []
        assert (
            DPIClient(_dpi).install(
                package="p.zip",
                version="1",
                name="N",
                on_progress=_seen.append,
            )
            is True
        )
        assert _seen == [0, 50, 100]

    def test_no_reboot_line_means_no_reboot(self, tmp_path: Path):
        """A payload that takes effect without one is legal — the agent bundle is
        exactly that — so the caller reads the DPI's answer instead of assuming."""
        _dpi = fake_dpi(tmp_path, "print('PROGRESS 100')")
        assert DPIClient(_dpi).install(package="p.zip", version="1", name="N") is False

    def test_a_refused_install_raises(self, tmp_path: Path):
        _dpi = fake_dpi(
            tmp_path,
            "print('the delta applies to a9ec… which is not what /dev/sda3 holds', file=sys.stderr)\n"
            "sys.exit(1)",
        )
        with pytest.raises(DPIError, match="exit 1"):
            DPIClient(_dpi).install(package="p.zip", version="1", name="N")


class TestWhichECUThisIs:
    """An image built for a vehicle carries one payload per ECU, and the DPI refuses
    to choose between them. otaclient knows which one this is; it has to say so."""

    def test_the_ecu_id_reaches_the_dpi(self, tmp_path: Path):
        _seen = tmp_path / "env"
        _dpi = fake_dpi(
            tmp_path,
            f"import os\n"
            f"open({str(_seen)!r}, 'w').write(os.environ.get('ROOTFS_UA_ECU_ID', '-') + ' '"
            f" + os.environ.get('ROOTFS_UA_RELEASE_KEY', '-'))\n"
            "print('VERSION 1.0.0')",
        )
        DPIClient(_dpi)._run(["get-version"], ecu_id="perception", release_key="prd")
        assert _seen.read_text() == "perception prd"

    def test_without_one_the_environment_is_left_alone(self, tmp_path: Path):
        """A device whose image carries a single payload needs no telling, and the
        DPI's own default stays in charge."""
        _seen = tmp_path / "env"
        _dpi = fake_dpi(
            tmp_path,
            f"import os\n"
            f"open({str(_seen)!r}, 'w').write(os.environ.get('ROOTFS_UA_ECU_ID', 'unset'))\n"
            "print('VERSION 1.0.0')",
        )
        DPIClient(_dpi).get_version()
        assert _seen.read_text() == "unset"


class TestResume:
    def test_resume_returns_the_committed_version(self, tmp_path: Path):
        _dpi = fake_dpi(tmp_path, "print('VERSION 2.9.0')")
        assert DPIClient(_dpi).resume() == "2.9.0"

    def test_a_failed_resume_raises(self, tmp_path: Path):
        """The DPI's verdict that this boot did not come up healthy is not otaclient's
        bug to hide: the caller turns it into a failed switch."""
        _dpi = fake_dpi(tmp_path, "sys.exit(1)")
        with pytest.raises(DPIError):
            DPIClient(_dpi).resume()


class TestLayout:
    def test_reads_the_slots_the_dpi_resolved(self, tmp_path: Path):
        _layout = {
            "active_slot": "rootfs_a",
            "standby_slot": "rootfs_b",
            "active_dev": "/dev/sda3",
            "standby_dev": "/dev/sda4",
        }
        _dpi = fake_dpi(tmp_path, f"print({json.dumps(json.dumps(_layout))})")
        assert DPIClient(_dpi).layout() == SlotLayout(
            active_slot="rootfs_a",
            standby_slot="rootfs_b",
            active_dev="/dev/sda3",
            standby_dev="/dev/sda4",
        )

    def test_the_dpis_log_on_stderr_does_not_reach_the_answer(self, tmp_path: Path):
        """The DPI logs what it resolved on stderr before it prints the JSON. Merging
        the two streams made every layout unreadable."""
        _layout = {
            "platform": "grub",
            "active_slot": "rootfs_a",
            "standby_slot": "rootfs_b",
            "active_dev": "/dev/sda3",
            "standby_dev": "/dev/sda4",
            "boot_dev": "/dev/sda2",
        }
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

    def test_an_unreadable_layout_is_an_error(self, tmp_path: Path):
        """Never guess a slot: writing the wrong partition is unrecoverable."""
        _dpi = fake_dpi(tmp_path, 'print(\'{"active_slot": "rootfs_a"}\')')
        with pytest.raises(DPIError, match="layout"):
            DPIClient(_dpi).layout()


def test_log_lines_do_not_become_protocol(tmp_path: Path, caplog):
    """Everything that is not one of the three words is a log line, including a line
    that merely mentions them."""
    _dpi = fake_dpi(
        tmp_path,
        "print('about to PROGRESS through the write')\nprint('VERSION 2.9.0')",
    )
    assert DPIClient(_dpi).get_version() == "2.9.0"
    assert "about to PROGRESS through the write" in caplog.text


def test_a_dynamically_loaded_client_runs_the_dpi_in_the_slots_own_root(
    tmp_path, monkeypatch
):
    """A dynamic otaclient runs with its own app image as root, and that image carries
    the client and nothing else. The DPI is the platform's, on the slot, rbound at
    /host_root — so it is run there. Without this, the boot
    controller could not even ask which slot it was on."""
    _calls = []

    def _fake_popen(cmd, **kwargs):
        _calls.append(cmd)
        raise OSError(2, "no such file")  # far enough: the command line is the point

    monkeypatch.setattr(_dpi.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(
        _dpi.otaclient_env, "get_dynamic_client_chroot_path", lambda: "/host_root"
    )

    with pytest.raises(DPIError):
        DPIClient(executable="/usr/local/sbin/rootfs-ua-dpi")._run(["get-version"])

    assert _calls == [
        ["chroot", "/host_root", "/usr/local/sbin/rootfs-ua-dpi", "get-version"]
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
        DPIClient(executable="/usr/local/sbin/rootfs-ua-dpi")._run(["get-version"])

    assert _calls == [["/usr/local/sbin/rootfs-ua-dpi", "get-version"]]


def test_the_wrapper_path_is_the_one_the_image_installs():
    """otaclient calls the privileged wrapper, not the bundle: the wrapper is what
    holds the sudoers rule and the rule that a delivered agent may supersede the
    image's."""
    from otaclient.boot_control._dpi import DEFAULT_DPI_PATH

    assert os.path.basename(DEFAULT_DPI_PATH) == "rootfs-ua-dpi"
