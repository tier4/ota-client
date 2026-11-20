# Copyright 2022 TIER IV, INC. All rights reserved.
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


from __future__ import annotations

from pytest_mock import MockerFixture

from otaclient_common import linux


def test_the_host_mount_namespace_is_entered_with_inits_root(mocker: MockerFixture):
    """Init's mount namespace and its root directory: where init was started from an
    initramfs and switched root, the namespace's root mount alone is not where the
    commands are."""
    mocker.patch.object(linux, "RUN_AS_PYINSTALLER_BUNDLE", False)
    _run = mocker.patch.object(linux.subprocess, "run")
    linux.subprocess_run_wrapper(
        ["mount", "--version"], check=True, check_output=False, set_host_mnt_ns=True
    )
    assert _run.call_args.args[0] == [
        "nsenter",
        "-t",
        "1",
        "-m",
        "-r",
        "mount",
        "--version",
    ]


def test_chroot_comes_before_entering_the_namespace(mocker: MockerFixture):
    mocker.patch.object(linux, "RUN_AS_PYINSTALLER_BUNDLE", False)
    _run = mocker.patch.object(linux.subprocess, "run")
    linux.subprocess_run_wrapper(
        ["findmnt", "/x"],
        check=True,
        check_output=False,
        chroot="/host_root",
        set_host_mnt_ns=True,
    )
    assert _run.call_args.args[0][:7] == [
        "chroot",
        "/host_root",
        "nsenter",
        "-t",
        "1",
        "-m",
        "-r",
    ]
