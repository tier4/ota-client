r"""Utils that shared between modules are listed here."""
import os
import shlex
import shutil
import subprocess
from hashlib import sha256
from pathlib import Path
from typing import Union

from app.log_util import get_logger
from app.configs import config as cfg

logger = get_logger(__name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL))

# file verification
def file_sha256(filename: Union[str, str]) -> str:
    with open(filename, "rb") as f:
        m = sha256()
        while True:
            d = f.read(cfg.LOCAL_CHUNK_SIZE)
            if len(d) == 0:
                break
            m.update(d)
        return m.hexdigest()


def verify_file(filename: Path, filehash: str, filesize) -> bool:
    if filesize and filename.stat().st_size != filesize:
        return False
    return file_sha256(filename) == filehash


# handled file read/write
def read_from_file(path: Path, *, missing_ok=True) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        if missing_ok:
            return ""

        raise


def write_to_file(path: Path, input: str):
    path.write_text(input)


# wrapped subprocess call
def subprocess_call(cmd: str, *, raise_exception=False):
    try:
        subprocess.check_call(shlex.split(cmd), stdout=subprocess.DEVNULL)
    except subprocess.CalledProcessError as e:
        logger.warning(
            msg=f"command failed(exit-code: {e.returncode} stderr: {e.stderr} stdout: {e.stdout}): {cmd}"
        )
        if raise_exception:
            raise


def subprocess_check_output(cmd: str, *, raise_exception=False, default="") -> str:
    try:
        return subprocess.check_output(shlex.split(cmd)).decode().strip()
    except subprocess.CalledProcessError as e:
        logger.warning(
            msg=f"command failed(exit-code: {e.returncode} stderr: {e.stderr} stdout: {e.stdout}): {cmd}"
        )
        if raise_exception:
            raise
        return default


def copy_stat(src: Union[Path, str], dst: Union[Path, str]):
    """Copy file/dir permission bits and owner info from src to dst."""
    _stat = Path(src).stat()
    os.chown(dst, _stat.st_uid, _stat.st_gid)
    os.chmod(dst, _stat.st_mode, follow_symlinks=False)


def dst_symlink_as_src(src: Path, dst: Path):
    """Make the dst a symlink link as src."""
    if dst.is_symlink() or dst.is_file():
        dst.unlink(missing_ok=True)

    dst.symlink_to(os.readlink(src))


def copytree_identical(src: Path, dst: Path):
    """Recursively copy from the src folder to dst folder.

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
    """
    if not dst.is_dir():
        raise FileNotFoundError(f"{dst} is not found or not a dir")

    # phase1: populate files to the dst
    for cur_dir, _, files in os.walk(src, topdown=True, followlinks=False):
        _cur_dir = Path(cur_dir)
        _cur_dir_on_dst = dst / _cur_dir.relative_to(src)

        # cover the edge case that dst is not a dir.
        if not _cur_dir_on_dst.is_dir():
            _cur_dir_on_dst.unlink(missing_ok=True)
            _cur_dir_on_dst.mkdir(parents=True)
            copy_stat(_cur_dir, _cur_dir_on_dst)

        # populate files
        for fname in files:
            _src_f = _cur_dir / fname
            _dst_f = _cur_dir_on_dst / fname

            # src and dst type mismatch, dst is a folder
            if _dst_f.is_dir():
                shutil.rmtree(_dst_f, ignore_errors=True)

            # check symlink, re-link if needed
            if _src_f.is_symlink():
                dst_symlink_as_src(_src_f, _dst_f)
                continue

            # finally populate the new file or override the dst file
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

        for fname in files:
            _src_f = _cur_dir_on_src / fname
            if not (_src_f.is_file() or _src_f.is_symlink()):
                (_cur_dir_on_dst / fname).unlink(missing_ok=True)
