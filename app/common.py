r"""Utils that shared between modules are listed here."""
import shlex
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
def read_from_file(path: Path) -> str:
    try:
        return path.read_text().strip()
    except Exception:
        return ""


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
