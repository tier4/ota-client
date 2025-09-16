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
"""Common shared helper functions for IO."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import sys
from functools import partial
from pathlib import Path

from otaclient_common._typing import StrOrPath
from otaclient_common.linux import is_directory

logger = logging.getLogger(__name__)

DEFAULT_FILE_CHUNK_SIZE = 1024**2  # 1MiB
TMP_FILE_PREFIX = ".ota_io_tmp_"


def _gen_tmp_fname(_prefix: str = TMP_FILE_PREFIX, _suffix_bytes: int = 6) -> str:
    return f"{_prefix}{os.urandom(_suffix_bytes).hex()}"


if sys.version_info >= (3, 11):
    from hashlib import file_digest as _file_digest

else:

    def _file_digest(
        fileobj: io.BufferedReader,
        digest,
        /,
        *,
        _bufsize: int = DEFAULT_FILE_CHUNK_SIZE,
    ) -> hashlib._Hash:
        """
        Basically a simpified copy from 3.11's hashlib.file_digest.
        """
        digestobj = hashlib.new(digest)

        buf = bytearray(_bufsize)  # Reusable buffer to reduce allocations.
        view = memoryview(buf)
        while True:
            size = fileobj.readinto(buf)
            if size == 0:
                break  # EOF
            digestobj.update(view[:size])

        return digestobj


def cal_file_digest(
    fpath: StrOrPath, algorithm: str, chunk_size: int = DEFAULT_FILE_CHUNK_SIZE
) -> str:
    """Generate file digest with <algorithm>.

    A wrapper for the _file_digest method.
    """
    return file_digest2(fpath, algorithm, chunk_size).hexdigest()


def file_digest2(
    fpath: StrOrPath, algorithm: str, chunk_size: int = DEFAULT_FILE_CHUNK_SIZE
) -> hashlib._Hash:
    """Generate file digest with <algorithm> and returns Hash object.

    A wrapper for the _file_digest method.
    """
    with open(fpath, "rb") as f:
        return _file_digest(f, algorithm, _bufsize=chunk_size)


file_sha256 = partial(cal_file_digest, algorithm="sha256")
file_sha256.__doc__ = "Generate file digest with sha256."

file_sha256_2 = partial(file_digest2, algorithm="sha256")
file_sha256.__doc__ = "Generate file digest with sha256, returns hash object."


def write_str_to_file_atomic(
    fpath: StrOrPath, _input: str, *, follow_symlink: bool = True
) -> None:
    """Overwrite the <fpath> with <_input> atomically.

    This function should be used to write small but critical files,
        like boot configuration files, etc.

    If <follow_symlink> is True and <fpath> is symlink, this method
        will get the realpath of the <fpath>, and then directly write to the realpath.

    NOTE: rename syscall is atomic when src and dst are on
    the same filesystem under linux.
    """
    if follow_symlink:
        fpath = Path(os.path.realpath(fpath))

    fpath_parent = Path(fpath).parent
    tmp_f = fpath_parent / _gen_tmp_fname()
    try:
        # ensure the new file is written
        with open(tmp_f, "w") as f:
            f.write(_input)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_f, fpath)  # atomically override
    finally:
        tmp_f.unlink(missing_ok=True)


def read_str_from_file(
    path: StrOrPath, _default: str | None = None
) -> str:  # pragma: no cover
    """Read contents as string from <path>.

    Args:
        _default: the default value to return when file is not found.

    Returns:
        The content of <path> in text, or <_default> if file is not found and <_default> is set.

    Raises:
        FileNotFoundError if <path> doesn't exist and <default> is None.
    """
    try:
        return Path(path).read_text().strip()
    except FileNotFoundError:
        if _default is not None:
            return _default
        raise


def symlink_atomic(src: StrOrPath, target: StrOrPath) -> None:
    """Make the <src> a symlink to <target> atomically.

    If the src doesn't exist, create the symlink.
    If the src is already existed as a file/symlink,
    the src will be replaced by the newly created symlink unconditionally.

    NOTE: os.rename/os.replace is atomic when src and dst are on
        the same filesystem under linux.

    Raises:
        IsADirectoryError if <src> exists and it is a directory.
        Any exceptions raised by Pathlib.symlink_to or os.rename.
    """
    src = Path(src)
    if src.is_symlink():
        if str(os.readlink(src)) == str(target):
            return  # the symlink is already correct
    elif not src.exists():  # NOTE: exists follow the symlink!
        return src.symlink_to(target)

    tmp_link = Path(src).parent / _gen_tmp_fname()
    try:
        tmp_link.symlink_to(target)
        os.replace(tmp_link, src)  # unconditionally replaced
    except Exception:
        tmp_link.unlink(missing_ok=True)
        raise


def copyfile_atomic(
    src: StrOrPath,
    dst: StrOrPath,
    *,
    follow_symlink: bool = True,
) -> None:
    """Atomically overwrite the <dst> file with <src> file.

    <src> must be presented and point to a file.
    <dst> should be absent or not a directory.

    Raises:
        Any exception raised by shutil.copy or os.replace.

    NOTE: atomic is ensured by os.rename/os.replace under the same filesystem.
    """
    _tmp_file = Path(dst).parent / _gen_tmp_fname()
    try:
        # prepare a copy of src file under dst's parent folder
        shutil.copy(src, _tmp_file, follow_symlinks=follow_symlink)
        # atomically rename/replace the dst file with the copy
        os.replace(_tmp_file, dst)
    finally:
        _tmp_file.unlink(missing_ok=True)


def remove_file(_fpath: StrOrPath, *, ignore_error: bool = True) -> None:
    """Use proper way to remove <_fpath>."""
    _fpath = Path(_fpath)
    if is_directory(_fpath):
        return shutil.rmtree(_fpath, ignore_errors=ignore_error)

    try:
        _fpath.unlink(missing_ok=True)
    except Exception:
        if not ignore_error:
            raise
