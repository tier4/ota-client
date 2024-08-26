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
"""Utils that shared between modules are listed here.

TODO(20240603): the old otaclient.app.common, split it by functionalities in
    the future.
"""


from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from hashlib import sha256
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urljoin

import requests

from otaclient_common.linux import subprocess_run_wrapper

logger = logging.getLogger(__name__)


def get_backoff(n: int, factor: float, _max: float) -> float:
    return min(_max, factor * (2 ** (n - 1)))


def wait_with_backoff(_retry_cnt: int, *, _backoff_factor: float, _backoff_max: float):
    time.sleep(
        get_backoff(
            _retry_cnt,
            _backoff_factor,
            _backoff_max,
        )
    )


# file verification
def file_sha256(
    filename: Union[Path, str], *, chunk_size: int = 1 * 1024 * 1024
) -> str:
    with open(filename, "rb") as f:
        m = sha256()
        while True:
            d = f.read(chunk_size)
            if len(d) == 0:
                break
            m.update(d)
        return m.hexdigest()


def verify_file(fpath: Path, fhash: str, fsize: Optional[int]) -> bool:
    if (
        fpath.is_symlink()
        or (not fpath.is_file())
        or (fsize is not None and fpath.stat().st_size != fsize)
    ):
        return False
    return file_sha256(fpath) == fhash


# handled file read/write
def read_str_from_file(path: Union[Path, str], *, missing_ok=True, default="") -> str:
    """
    Params:
        missing_ok: if set to False, FileNotFoundError will be raised to upper
        default: the default value to return when missing_ok=True and file not found
    """
    try:
        return Path(path).read_text().strip()
    except FileNotFoundError:
        if missing_ok:
            return default

        raise


def write_str_to_file(path: Path, _input: str):
    path.write_text(_input)


def write_str_to_file_sync(path: Union[Path, str], _input: str):
    with open(path, "w") as f:
        f.write(_input)
        f.flush()
        os.fsync(f.fileno())


def subprocess_check_output(
    cmd: str | list[str],
    *,
    raise_exception: bool = False,
    default: str = "",
    timeout: Optional[float] = None,
) -> str:
    """Run the <cmd> and return UTF-8 decoded stripped stdout.

    Args:
        cmd (str | list[str]): command to be executed.
        raise_exception (bool, optional): raise the underlying CalledProcessError. Defaults to False.
        default (str, optional): if <raise_exception> is False, return <default> on underlying
            subprocess call failed. Defaults to "".
        timeout (Optional[float], optional): timeout for execution. Defaults to None.

    Returns:
        str: UTF-8 decoded stripped stdout.
    """
    try:
        res = subprocess_run_wrapper(
            cmd, check=True, check_output=True, timeout=timeout
        )
        return res.stdout.decode().strip()
    except subprocess.CalledProcessError as e:
        _err_msg = (
            f"command({cmd=}) failed(retcode={e.returncode}: \n"
            f"stderr={e.stderr.decode()}"
        )
        logger.debug(_err_msg)

        if raise_exception:
            raise
        return default


def subprocess_call(
    cmd: str | list[str],
    *,
    raise_exception: bool = False,
    timeout: Optional[float] = None,
) -> None:
    """Run the <cmd>.

    Args:
        cmd (str | list[str]): command to be executed.
        raise_exception (bool, optional): raise the underlying CalledProcessError. Defaults to False.
        timeout (Optional[float], optional): timeout for execution. Defaults to None.
    """
    try:
        subprocess_run_wrapper(cmd, check=True, check_output=False, timeout=timeout)
    except subprocess.CalledProcessError as e:
        _err_msg = (
            f"command({cmd=}) failed(retcode={e.returncode}: \n"
            f"stderr={e.stderr.decode()}"
        )
        logger.debug(_err_msg)

        if raise_exception:
            raise


def copy_stat(src: Union[Path, str], dst: Union[Path, str]):
    """Copy file/dir permission bits and owner info from src to dst."""
    _stat = Path(src).stat()
    os.chown(dst, _stat.st_uid, _stat.st_gid)
    os.chmod(dst, _stat.st_mode)


def copytree_identical(src: Path, dst: Path):
    """Recursively copy from the src folder to dst folder.

    Source folder MUST be a dir.

    This function populate files/dirs from the src to the dst,
    and make sure the dst is identical to the src.

    By updating the dst folder in-place, we can prevent the case
    that the copy is interrupted and the dst is not yet fully populated.

    This function is different from shutil.copytree as follow:
    1. it covers the case that the same path points to different
        file type, in this case, the dst path will be clean and
        new file/dir will be populated as the src.
    2. it deals with the same symlinks by checking the link target,
        re-generate the symlink if the dst symlink is not the same
        as the src.
    3. it will remove files that not presented in the src, and
        unconditionally override files with same path, ensuring
        that the dst will be identical with the src.

    NOTE: is_file/is_dir also returns True if it is a symlink and
    the link target is_file/is_dir
    """
    if src.is_symlink() or not src.is_dir():
        raise ValueError(f"{src} is not a dir")

    if dst.is_symlink() or not dst.is_dir():
        logger.info(f"{dst=} doesn't exist or not a dir, cleanup and mkdir")
        dst.unlink(missing_ok=True)  # unlink doesn't follow the symlink
        dst.mkdir(mode=src.stat().st_mode, parents=True)

    # phase1: populate files to the dst
    for cur_dir, dirs, files in os.walk(src, topdown=True, followlinks=False):
        _cur_dir = Path(cur_dir)
        _cur_dir_on_dst = dst / _cur_dir.relative_to(src)

        # NOTE(20220803): os.walk now lists symlinks pointed to dir
        # in the <dirs> tuple, we have to handle this behavior
        for _dir in dirs:
            _src_dir = _cur_dir / _dir
            _dst_dir = _cur_dir_on_dst / _dir
            if _src_dir.is_symlink():  # this "dir" is a symlink to a dir
                if (not _dst_dir.is_symlink()) and _dst_dir.is_dir():
                    # if dst is a dir, remove it
                    shutil.rmtree(_dst_dir, ignore_errors=True)
                else:  # dst is symlink or file
                    _dst_dir.unlink()
                _dst_dir.symlink_to(os.readlink(_src_dir))

        # cover the edge case that dst is not a dir.
        if _cur_dir_on_dst.is_symlink() or not _cur_dir_on_dst.is_dir():
            _cur_dir_on_dst.unlink(missing_ok=True)
            _cur_dir_on_dst.mkdir(parents=True)
            copy_stat(_cur_dir, _cur_dir_on_dst)

        # populate files
        for fname in files:
            _src_f = _cur_dir / fname
            _dst_f = _cur_dir_on_dst / fname

            # prepare dst
            #   src is file but dst is a folder
            #   delete the dst in advance
            if (not _dst_f.is_symlink()) and _dst_f.is_dir():
                # if dst is a dir, remove it
                shutil.rmtree(_dst_f, ignore_errors=True)
            else:
                # dst is symlink or file
                _dst_f.unlink(missing_ok=True)

            # copy/symlink dst as src
            #   if src is symlink, check symlink, re-link if needed
            if _src_f.is_symlink():
                _dst_f.symlink_to(os.readlink(_src_f))
            else:
                # copy/override src to dst
                shutil.copy(_src_f, _dst_f, follow_symlinks=False)
                copy_stat(_src_f, _dst_f)

    # phase2: remove unused files in the dst
    for cur_dir, dirs, files in os.walk(dst, topdown=True, followlinks=False):
        _cur_dir_on_dst = Path(cur_dir)
        _cur_dir_on_src = src / _cur_dir_on_dst.relative_to(dst)

        # remove unused dir
        if not _cur_dir_on_src.is_dir():
            shutil.rmtree(_cur_dir_on_dst, ignore_errors=True)
            dirs.clear()  # stop iterate the subfolders of this dir
            continue

        # NOTE(20220803): os.walk now lists symlinks pointed to dir
        # in the <dirs> tuple, we have to handle this behavior
        for _dir in dirs:
            _src_dir = _cur_dir_on_src / _dir
            _dst_dir = _cur_dir_on_dst / _dir
            if (not _src_dir.is_symlink()) and _dst_dir.is_symlink():
                _dst_dir.unlink()

        for fname in files:
            _src_f = _cur_dir_on_src / fname
            if not (_src_f.is_symlink() or _src_f.is_file()):
                (_cur_dir_on_dst / fname).unlink(missing_ok=True)


def re_symlink_atomic(src: Path, target: Union[Path, str]):
    """Make the <src> a symlink to <target> atomically.

    If the src is already existed as a file/symlink,
    the src will be replaced by the newly created link unconditionally.

    NOTE: os.rename is atomic when src and dst are on
    the same filesystem under linux.
    NOTE 2: src should not exist or exist as file/symlink.
    """
    if not (src.is_symlink() and str(os.readlink(src)) == str(target)):
        tmp_link = Path(src).parent / f"tmp_link_{os.urandom(6).hex()}"
        try:
            tmp_link.symlink_to(target)
            os.rename(tmp_link, src)  # unconditionally override
        except Exception:
            tmp_link.unlink(missing_ok=True)
            raise


def replace_atomic(src: Union[str, Path], dst: Union[str, Path]):
    """Atomically replace dst file with src file.

    NOTE: atomic is ensured by os.rename/os.replace under the same filesystem.
    """
    src, dst = Path(src), Path(dst)
    if not src.is_file():
        raise ValueError(f"{src=} is not a regular file or not exist")

    _tmp_file = dst.parent / f".tmp_{os.urandom(6).hex()}"
    try:
        # prepare a copy of src file under dst's parent folder
        shutil.copy(src, _tmp_file, follow_symlinks=True)
        # atomically rename/replace the dst file with the copy
        os.replace(_tmp_file, dst)
        os.sync()
    except Exception:
        _tmp_file.unlink(missing_ok=True)
        raise


def urljoin_ensure_base(base: str, url: str):
    """
    NOTE: this method ensure the base_url will be preserved.
          for example:
            base="http://example.com/data", url="path/to/file"
          with urljoin, joined url will be "http://example.com/path/to/file",
          with this func, joined url will be "http://example.com/data/path/to/file"
    """
    return urljoin(f"{base.rstrip('/')}/", url)


def create_tmp_fname(prefix="tmp", length=6, sep="_") -> str:
    return f"{prefix}{sep}{os.urandom(length).hex()}"


def ensure_otaproxy_start(
    otaproxy_url: str,
    *,
    interval: float = 1,
    connection_timeout: float = 5,
    probing_timeout: Optional[float] = None,
    warning_interval: int = 3 * 60,  # seconds
):
    """Loop probing <otaproxy_url> until online or exceed <probing_timeout>.

    This function will issue a logging.warning every <warning_interval> seconds.

    Raises:
        A ConnectionError if exceeds <probing_timeout>.
    """
    start_time = int(time.time())
    next_warning = start_time + warning_interval
    probing_timeout = (
        probing_timeout if probing_timeout and probing_timeout >= 0 else float("inf")
    )
    with requests.Session() as session:
        while start_time + probing_timeout > (cur_time := int(time.time())):
            try:
                resp = session.get(otaproxy_url, timeout=connection_timeout)
                resp.close()
                return
            except Exception as e:  # server is not up yet
                if cur_time >= next_warning:
                    logger.warning(
                        f"otaproxy@{otaproxy_url} is not up after {cur_time - start_time} seconds"
                        f"it might be something wrong with this otaproxy: {e!r}"
                    )
                    next_warning = next_warning + warning_interval
                time.sleep(interval)
    raise ConnectionError(
        f"failed to ensure connection to {otaproxy_url} in {probing_timeout=}seconds"
    )
