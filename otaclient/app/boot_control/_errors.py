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
r"""Internal error types used by boot_control module."""

from enum import Enum, auto
from subprocess import CalledProcessError
from typing import Optional, Callable


class MountFailedReason(Enum):
    # error code
    SUCCESS = 0
    PERMISSIONS_ERROR = 1
    SYSTEM_ERROR = 2
    INTERNAL_ERROR = 4
    USER_INTERRUPT = 8
    GENERIC_MOUNT_FAILURE = 32

    # custom error code
    # specific reason for generic mount failure
    TARGET_NOT_FOUND = auto()
    TARGET_ALREADY_MOUNTED = auto()
    MOUNT_POINT_NOT_FOUND = auto()
    BIND_MOUNT_ON_NON_DIR = auto()

    @classmethod
    def parse_failed_reason(cls, func: Callable):
        from functools import wraps

        @wraps(func)
        def _wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except CalledProcessError as e:
                _called_process_error_str = (
                    f"{e.cmd=} failed: {e.returncode=}, {e.stderr=}, {e.stdout=}"
                )

                _reason = cls(e.returncode)
                if _reason != cls.GENERIC_MOUNT_FAILURE:
                    raise MountError(
                        _called_process_error_str,
                        fail_reason=_reason,
                    ) from None

                # if the return code is 32, determine the detailed reason
                # of the mount failure
                _error_msg, _fail_reason = str(e.stderr), cls.GENERIC_MOUNT_FAILURE
                if _error_msg.find("already mounted") != -1:
                    _fail_reason = cls.TARGET_ALREADY_MOUNTED
                elif _error_msg.find("mount point does not exist") != -1:
                    _fail_reason = cls.MOUNT_POINT_NOT_FOUND
                elif _error_msg.find("does not exist") != -1:
                    _fail_reason = cls.TARGET_NOT_FOUND
                elif _error_msg.find("Not a directory") != -1:
                    _fail_reason = cls.BIND_MOUNT_ON_NON_DIR

                raise MountError(
                    _called_process_error_str,
                    fail_reason=_fail_reason,
                ) from None

        return _wrapper


class BootControlError(Exception):
    """Internal used exception type related to boot control errors.
    NOTE: this exception should not be directly raised to upper caller,
    it should always be wrapped by a specific OTA Error type.
    check app.errors for details.
    """


class MountError(BootControlError):
    def __init__(
        self, *args: object, fail_reason: Optional["MountFailedReason"] = None
    ) -> None:
        super().__init__(*args)
        self.fail_reason = fail_reason


class ABPartitionError(BootControlError):
    ...


class MkfsError(BootControlError):
    ...
