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
import os
import shutil
import stat
import sys
from functools import partial
from pathlib import Path

from otaclient_common._typing import StrOrPath
from otaclient_common.linux import is_directory

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
    return digest_file(fpath, algorithm, chunk_size).hexdigest()


def digest_file(
    fpath: StrOrPath, algorithm: str, chunk_size: int = DEFAULT_FILE_CHUNK_SIZE
) -> hashlib._Hash:
    """Generate file digest with <algorithm> and returns Hash object.

    A wrapper for the _file_digest method.
    """
    with open(fpath, "rb") as f:
        return _file_digest(f, algorithm, _bufsize=chunk_size)


file_sha256 = partial(cal_file_digest, algorithm="sha256")
file_sha256.__doc__ = "Generate file digest with sha256."

file_sha256_digest = partial(digest_file, algorithm="sha256")
file_sha256_digest.__doc__ = "Generate file digest with sha256, returns hash object."


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


def _mount_point_of(path: Path) -> Path:
    """The mount point of the filesystem <path> is on, or would be on once made: its
    nearest ancestor, itself included, that is a mount point."""
    while not os.path.ismount(path) and path != path.parent:
        path = path.parent
    return path


def ensure_private_dir(
    path: StrOrPath, *, mode: int = 0o755, create: bool = True
) -> Path:
    """Make sure <path> is a directory that only root and this process's user can
    change, on a path that only they can change either.

    For a process that writes and removes things under <path> as root: a symlink on
    the way, or a directory another user owns or can write to, would let that user
    point those writes and removals at any file. So every component from the mount
    point <path> is on down to <path> itself is lstat'ed, and each has to be a
    directory (a symlink is refused, never followed), owned by root or by the
    effective uid, and not writable by group or others. What is above the mount
    point does not matter: a mount point cannot be renamed or replaced but by root.

    What is missing is created as it is reached, after everything above it passed:
    <path> itself with <mode>, a directory on the way to it with 0o755, neither
    group- or world-writable whatever the umask. With <create> False the check stops
    at the first component that does not exist, and nothing is created.

    Returns:
        <path>, made absolute.

    Raises:
        PermissionError: a component is a symlink, not a directory, or not private
            in the sense above. Nothing below it is looked at or created.
        ValueError: <mode> is group- or world-writable.
    """
    if mode & 0o022:
        raise ValueError(f"mode {mode:o} would make {path} group- or world-writable")
    _path = Path(os.path.abspath(path))
    _top = _mount_point_of(_path)
    _euid = os.geteuid()

    _components = [_top]
    for _part in _path.relative_to(_top).parts:
        _components.append(_components[-1] / _part)

    for _dir in _components:
        try:
            _st = os.lstat(_dir)
        except FileNotFoundError:
            if not create:
                break
            try:
                os.mkdir(_dir, mode if _dir == _path else 0o755)
            except FileExistsError:
                pass  # made meanwhile, by whoever could: checked as it is
            _st = os.lstat(_dir)
        if stat.S_ISLNK(_st.st_mode):
            raise PermissionError(f"{_dir} is a symlink")
        if not stat.S_ISDIR(_st.st_mode):
            raise PermissionError(f"{_dir} is not a directory")
        if _st.st_uid not in (0, _euid) or _st.st_mode & 0o022:
            raise PermissionError(
                f"{_dir} is not a private directory (owner uid {_st.st_uid}, mode "
                f"{stat.S_IMODE(_st.st_mode):o}): it has to be root's or ours, and "
                "not writable by group or others"
            )
    return _path
