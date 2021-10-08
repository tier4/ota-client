import shutil
from pathlib import Path
from logging import getLogger

import configs as cfg

logger = getLogger(__name__)
logger.setLevel(cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL))


def _copy_preserve(src: Path, dst_dir: Path):
    if not dst_dir.is_dir() or dst_dir.is_symlink():
        raise OtaErrorUnrecoverable(f"{dst_dir} should be plain directory")

    dst_path = dst_dir / src.name
    # src is plain directory?
    if src.is_dir() and not src.is_symlink():
        # if plain file or symlink (which links to file or directory)
        if dst_path.is_file() or dst_path.is_symlink():
            raise OtaErrorRecoverable(f"{dst_path} exists but is not a directory")
        if dst_path.is_dir():  # dst_path exists as a directory
            return  # keep it untouched
        logger.info(f"creating directory {dst_path}")
        dst_path.mkdir()  # FIXME mode/owner
    # src is plain file or symlink
    elif src.is_file() or src.is_symlink():  # includes broken symlink
        logger.info(f"copying file {dst_dir / src.name}")
        shutil.copy2(src, dst_dir, follow_symlinks=False)
        # FIXME mode/owner
    else:
        raise OtaErrorUnrecoverable(f"{src} unintended file type")


def _copy_recursive(src: Path, dst_dir: Path):
    _copy_preserve(src, dst_dir)
    if src.is_dir() and not src.is_symlink():
        for src_child in src.glob("*"):
            _copy_recursive(src_child, dst_dir / src.name)


def copy_parents(src: Path, dst_dir: Path):
    dst_path = dst_dir
    if not dst_path.is_dir() or dst_path.is_symlink():
        raise OtaErrorUnrecoverable(f"{dst_path} should be plain directory")
    for parent in reversed(list(src.parents)):
        _copy_preserve(parent, dst_path)
        dst_path = dst_path / parent.name
    _copy_recursive(src, dst_path)
