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
"""Tests for common boot control base class."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn
from unittest.mock import MagicMock

import pytest

from otaclient import errors as ota_errors
from otaclient._types import OTAStatus
from otaclient.boot_control._base import BootControllerBase
from otaclient.boot_control._ota_status_control import OTAStatusFilesControl
from otaclient.boot_control._slot_mnt_helper import SlotMountHelper


class ConcreteBootController(BootControllerBase):
    """Concrete implementation for testing the base class."""

    def __init__(self):
        # Mock the required components
        self._mp_control = MagicMock(spec=SlotMountHelper)
        self._mp_control.standby_slot_dev = "/dev/mock_standby"
        self._mp_control.standby_slot_mount_point = Path("/mnt/standby")

        self._ota_status_control = MagicMock(spec=OTAStatusFilesControl)
        self._ota_status_control.load_active_slot_version.return_value = "1.0.0"
        self._ota_status_control.booted_ota_status = OTAStatus.SUCCESS

        # Track platform-specific method calls
        self.pre_update_platform_called = False
        self.post_update_platform_called = False

    @property
    def bootloader_type(self) -> str:
        return "mock_bootloader"

    def _pre_update_platform_specific(
        self, *, standby_as_ref: bool, erase_standby: bool
    ) -> None:
        self.pre_update_platform_called = True

    def _post_update_platform_specific(self, *, update_version: str) -> None:
        self.post_update_platform_called = True

    def finalizing_update(self, *, chroot: str | None = None) -> NoReturn:
        raise SystemExit("mock reboot")


class TestBootControllerBase:
    """Test suite for BootControllerBase class."""

    def test_common_properties(self):
        """Test that common properties are correctly implemented."""
        controller = ConcreteBootController()

        # Test standby_slot_dev property
        assert controller.standby_slot_dev == Path("/dev/mock_standby")

        # Test get_standby_slot_path
        assert controller.get_standby_slot_path() == Path("/mnt/standby")

        # Test get_standby_slot_dev
        assert controller.get_standby_slot_dev() == "/dev/mock_standby"

        # Test load_version
        assert controller.load_version() == "1.0.0"
        controller._ota_status_control.load_active_slot_version.assert_called_once()

        # Test get_booted_ota_status
        assert controller.get_booted_ota_status() == OTAStatus.SUCCESS

        # Test bootloader_type
        assert controller.bootloader_type == "mock_bootloader"

    def test_on_operation_failure(self):
        """Test that on_operation_failure calls the expected methods."""
        controller = ConcreteBootController()

        controller.on_operation_failure()

        # Verify that failure cleanup methods are called
        controller._ota_status_control.on_failure.assert_called_once()
        controller._mp_control.umount_all.assert_called_once()

    def test_on_abort(self):
        """Test that on_abort calls the expected methods."""
        controller = ConcreteBootController()

        controller.on_abort()

        # Verify that abort cleanup methods are called
        controller._ota_status_control.on_abort.assert_called_once()
        controller._mp_control.umount_all.assert_called_once()

    def test_pre_update_success(self):
        """Test pre_update template method with successful execution."""
        controller = ConcreteBootController()

        controller.pre_update(standby_as_ref=False, erase_standby=True)

        # Verify the common flow steps were executed
        controller._ota_status_control.pre_update_current.assert_called_once()
        controller._mp_control.prepare_standby_dev.assert_called_once()
        controller._mp_control.mount_standby.assert_called_once()
        controller._mp_control.mount_active.assert_called_once()

        # Verify platform-specific method was called
        assert controller.pre_update_platform_called is True

    def test_pre_update_failure_handling(self):
        """Test pre_update error handling."""
        controller = ConcreteBootController()

        # Simulate failure in mount_standby
        controller._mp_control.mount_standby.side_effect = Exception("Mount failed")

        with pytest.raises(ota_errors.BootControlPreUpdateFailed) as exc_info:
            controller.pre_update(standby_as_ref=False, erase_standby=False)

        assert "failed on pre_update" in str(exc_info.value)

    def test_post_update_success(self):
        """Test post_update template method with successful execution."""
        controller = ConcreteBootController()

        controller.post_update(update_version="2.0.0")

        # Verify the common flow steps were executed
        controller._ota_status_control.post_update_standby.assert_called_once()
        controller._mp_control.umount_all.assert_called_once()

        # Verify platform-specific method was called
        assert controller.post_update_platform_called is True

    def test_post_update_failure_handling(self):
        """Test post_update error handling."""
        controller = ConcreteBootController()

        # Simulate failure in platform-specific code
        def raise_error(**kwargs):
            raise ValueError("Platform-specific error")

        controller._post_update_platform_specific = raise_error

        with pytest.raises(ota_errors.BootControlPostUpdateFailed) as exc_info:
            controller.post_update(update_version="2.0.0")

        assert "failed on post_update" in str(exc_info.value)

    def test_template_method_execution_order(self):
        """Test that template methods execute steps in correct order."""
        controller = ConcreteBootController()
        call_order = []

        # Track call order
        controller._ota_status_control.pre_update_current.side_effect = (
            lambda: call_order.append("pre_update_current")
        )
        controller._mp_control.prepare_standby_dev.side_effect = (
            lambda **kwargs: call_order.append("prepare_standby_dev")
        )
        controller._mp_control.mount_standby.side_effect = lambda: call_order.append(
            "mount_standby"
        )
        controller._mp_control.mount_active.side_effect = lambda: call_order.append(
            "mount_active"
        )

        original_platform_method = controller._pre_update_platform_specific

        def tracked_platform_method(**kwargs):
            call_order.append("platform_specific")
            original_platform_method(**kwargs)

        controller._pre_update_platform_specific = tracked_platform_method

        controller.pre_update(standby_as_ref=False, erase_standby=False)

        # Verify execution order
        expected_order = [
            "pre_update_current",
            "prepare_standby_dev",
            "mount_standby",
            "mount_active",
            "platform_specific",
        ]
        assert call_order == expected_order

    def test_default_pre_update_prepare_standby(self):
        """Test default implementation of _pre_update_prepare_standby."""
        controller = ConcreteBootController()

        # Call the default implementation directly
        controller._pre_update_prepare_standby(erase_standby=True)

        # Should delegate to SlotMountHelper
        controller._mp_control.prepare_standby_dev.assert_called_once()

    def test_platform_specific_hooks_optional(self):
        """Test that platform-specific hooks are optional (have default implementations)."""

        class MinimalBootController(BootControllerBase):
            """Minimal implementation with only required methods."""

            def __init__(self):
                self._mp_control = MagicMock(spec=SlotMountHelper)
                self._mp_control.standby_slot_dev = "/dev/test"
                self._mp_control.standby_slot_mount_point = Path("/mnt/test")
                self._ota_status_control = MagicMock(spec=OTAStatusFilesControl)

            @property
            def bootloader_type(self) -> str:
                return "minimal"

            def _post_update_platform_specific(self, *, update_version: str) -> None:
                pass

            def finalizing_update(self, *, chroot: str | None = None) -> NoReturn:
                raise SystemExit("reboot")

        # Should not raise any errors
        controller = MinimalBootController()
        controller.pre_update(standby_as_ref=False, erase_standby=False)
        # The default implementation (_pre_update_platform_specific)
        # should do nothing without raising errors
