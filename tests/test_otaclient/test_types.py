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
"""Tests for otaclient._types module."""

from __future__ import annotations

import multiprocessing as mp
import threading
import time

import pytest

from otaclient._types import AbortThreadLock, CriticalZoneFlag


class TestCriticalZoneFlag:
    """Tests for CriticalZoneFlag class."""

    @pytest.fixture
    def critical_zone_flag(self):
        """Create a CriticalZoneFlag with a multiprocessing lock."""
        ctx = mp.get_context("spawn")
        lock = ctx.Lock()
        return CriticalZoneFlag(lock)

    def test_acquire_lock_with_release_non_blocking_success(self, critical_zone_flag):
        """Test non-blocking acquire when lock is available."""
        with critical_zone_flag.acquire_lock_with_release(blocking=False) as acquired:
            assert acquired is True

    def test_acquire_lock_with_release_non_blocking_fail(self, critical_zone_flag):
        """Test non-blocking acquire when lock is held."""
        # First acquire the lock
        critical_zone_flag.acquire_lock_no_release(blocking=False)

        # Try to acquire again - should fail
        with critical_zone_flag.acquire_lock_with_release(blocking=False) as acquired:
            assert acquired is False

    def test_acquire_lock_with_release_releases_lock(self, critical_zone_flag):
        """Test that lock is released after exiting context."""
        with critical_zone_flag.acquire_lock_with_release(blocking=False) as acquired:
            assert acquired is True

        # Lock should be released, so we can acquire again
        with critical_zone_flag.acquire_lock_with_release(blocking=False) as acquired:
            assert acquired is True

    def test_acquire_lock_with_release_blocking(self, critical_zone_flag):
        """Test blocking acquire waits for lock."""
        results = []

        def holder():
            critical_zone_flag.acquire_lock_no_release(blocking=True)
            time.sleep(0.2)
            # Note: We need to release manually since we used acquire_lock_no_release
            critical_zone_flag._lock.release()

        def waiter():
            # This should block until holder releases
            with critical_zone_flag.acquire_lock_with_release(
                blocking=True
            ) as acquired:
                results.append(acquired)

        holder_thread = threading.Thread(target=holder)
        waiter_thread = threading.Thread(target=waiter)

        holder_thread.start()
        time.sleep(0.05)  # Ensure holder has the lock
        waiter_thread.start()

        holder_thread.join()
        waiter_thread.join()

        assert results == [True]

    def test_acquire_lock_no_release_does_not_release(self, critical_zone_flag):
        """Test that acquire_lock_no_release doesn't release the lock."""
        acquired = critical_zone_flag.acquire_lock_no_release(blocking=False)
        assert acquired is True

        # Try to acquire again - should fail because lock is still held
        acquired_again = critical_zone_flag.acquire_lock_no_release(blocking=False)
        assert acquired_again is False


class TestAbortThreadLock:
    """Tests for AbortThreadLock class."""

    @pytest.fixture
    def abort_thread_lock(self):
        """Create an AbortThreadLock."""
        return AbortThreadLock()

    def test_acquire_lock_with_release_non_blocking_success(self, abort_thread_lock):
        """Test non-blocking acquire when lock is available."""
        with abort_thread_lock.acquire_lock_with_release(blocking=False) as acquired:
            assert acquired is True

    def test_acquire_lock_with_release_non_blocking_fail(self, abort_thread_lock):
        """Test non-blocking acquire when lock is held."""
        # First acquire the lock
        abort_thread_lock.acquire_lock_no_release(blocking=False)

        # Try to acquire again - should fail
        with abort_thread_lock.acquire_lock_with_release(blocking=False) as acquired:
            assert acquired is False

        # Clean up
        abort_thread_lock.release_lock()

    def test_acquire_lock_with_release_releases_lock(self, abort_thread_lock):
        """Test that lock is released after exiting context."""
        with abort_thread_lock.acquire_lock_with_release(blocking=False) as acquired:
            assert acquired is True

        # Lock should be released, so we can acquire again
        with abort_thread_lock.acquire_lock_with_release(blocking=False) as acquired:
            assert acquired is True

    def test_acquire_lock_no_release_does_not_release(self, abort_thread_lock):
        """Test that acquire_lock_no_release doesn't release the lock."""
        acquired = abort_thread_lock.acquire_lock_no_release(blocking=False)
        assert acquired is True

        # Try to acquire again - should fail because lock is still held
        acquired_again = abort_thread_lock.acquire_lock_no_release(blocking=False)
        assert acquired_again is False

        # Clean up
        abort_thread_lock.release_lock()

    def test_release_lock(self, abort_thread_lock):
        """Test explicit lock release."""
        abort_thread_lock.acquire_lock_no_release(blocking=False)

        # Release the lock
        abort_thread_lock.release_lock()

        # Should be able to acquire again
        acquired = abort_thread_lock.acquire_lock_no_release(blocking=False)
        assert acquired is True

        # Clean up
        abort_thread_lock.release_lock()

    def test_acquire_lock_with_release_blocking(self, abort_thread_lock):
        """Test blocking acquire waits for lock."""
        results = []

        def holder():
            abort_thread_lock.acquire_lock_no_release(blocking=True)
            time.sleep(0.2)
            abort_thread_lock.release_lock()

        def waiter():
            # This should block until holder releases
            with abort_thread_lock.acquire_lock_with_release(blocking=True) as acquired:
                results.append(acquired)

        holder_thread = threading.Thread(target=holder)
        waiter_thread = threading.Thread(target=waiter)

        holder_thread.start()
        time.sleep(0.05)  # Ensure holder has the lock
        waiter_thread.start()

        holder_thread.join()
        waiter_thread.join()

        assert results == [True]
