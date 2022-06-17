import grp
import os
import pwd
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

from app import log_util
from app.configs import config as cfg
from app.ota_error import OtaErrorUnrecoverable

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


@dataclass
class PwdEntry:
    pw_name: str
    pw_uid: int
    pw_gid: int

    def __init__(self, pw_line: str) -> None:
        """
        typical pw_line format:
            <name>:<passwd>:<uid>:<gid>:<comment>:<home>:<shell>
            root:x:0:0:root:/root:/bin/bash
        """
        _parsed = pw_line.split(":")
        self.pw_name = _parsed[0]
        self.pw_uid = _parsed[2]
        self.pw_gid = _parsed[3]


@dataclass
class GrpEntry:
    gr_name: str
    gr_id: int

    def __init__(self, grp_line: str) -> None:
        """
        typical grp_line format:
            <group_nmae>:<passwd>:<gid>:<list_of_username>
            adm:x:4:syslog,autoware
        """
        _parsed = grp_line.split(":")
        self.gr_name = _parsed[0]
        self.gr_id = _parsed[2]


def _load_dst_pwd(dst_pwd: Path) -> Dict[str, PwdEntry]:
    # name -> uid
    _uname_uid_mapping: Dict[str, PwdEntry] = {}
    with open(dst_pwd, "r") as f:
        for l in f:
            _parsed = PwdEntry(l)
            _uname_uid_mapping[_parsed.pw_name] = _parsed

    return _uname_uid_mapping


def _load_dst_grp(dst_group: Path) -> Dict[str, GrpEntry]:
    # name -> uid
    _gname_gid_mapping: Dict[str, GrpEntry] = []
    with open(dst_group, "r") as f:
        for l in f:
            _parsed = GrpEntry(l)
            _gname_gid_mapping[_parsed.gr_name] = _parsed

    return _gname_gid_mapping


class CopyTree:
    def __init__(
        self,
        dst_passwd_file: Path,
        dst_group_file: Path,
    ):
        # cache
        self._src_uid_uname_mapping: Dict[int, str] = {}
        self._src_gid_gname_mapping: Dict[int, str] = {}

        # src uname to dst uid
        self._dst_uname_uid_mapping: Dict[str, PwdEntry] = _load_dst_pwd(
            dst_passwd_file
        )
        # src gname to dst gid
        self._dst_gname_gid_mapping: Dict[str, GrpEntry] = _load_dst_grp(dst_group_file)

    def copy_with_parents(self, src: Path, dst_dir: Path):
        dst_path = dst_dir
        if not dst_path.is_dir() or dst_path.is_symlink():
            raise ValueError(f"{dst_path} should be plain directory")

        for parent in reversed(list(src.parents)):
            self._copy_preserve(parent, dst_path)
            dst_path = dst_path / parent.name
        self._copy_recursive(src, dst_path)

    """ private functions from here """

    def _map_uid_gid(self, src: Path) -> Tuple[int, int]:
        _stat = src.stat()
        _src_uid, _src_gid = _stat.st_uid, _stat.st_gid

        if _src_uid in self._src_uid_uname_mapping:
            _src_uname = self._src_uid_uname_mapping[_src_uid]
        else:
            _src_uname = self._src_uid_uname_mapping.setdefault(
                _src_uid, pwd.getpwnam(_src_uid).pw_name
            )
        _dst_uid = self._dst_uname_uid_mapping[_src_uname].pw_uid

        if _src_gid in self._src_gid_gname_mapping:
            _src_gname = self._src_gid_gname_mapping[_src_gid]
        else:
            _src_gname = self._src_gid_gname_mapping.setdefault(
                _src_gid, grp.getgrnam(_src_gid).gr_name
            )
        _dst_gid = self._dst_gname_gid_mapping[_src_gname].gr_id

        return _dst_uid, _dst_gid

    def _copy_stat(self, src: Path, dst: Path):
        st = src.stat()
        try:
            dst_uid, dst_gid = self._map_uid_gid(src)
        except KeyError:  # In case src UID/GID not found, keep UID/GID as is.
            logger.warning(
                f"src_uid: {st.st_uid}, src_gid: {st.st_gid} mapping not found"
            )
            dst_uid, dst_gid = st.st_uid, st.st_gid

        os.chown(dst, dst_uid, dst_gid, follow_symlinks=False)
        if not dst.is_symlink():  # symlink always 777
            os.chmod(dst, st.st_mode)

    def _copy_preserve(self, src: Path, dst_dir: Path):
        if not dst_dir.is_dir() or dst_dir.is_symlink():
            raise OtaErrorUnrecoverable(f"{dst_dir} should be plain directory")

        dst_path = dst_dir / src.name
        # src is plain directory?
        if src.is_dir() and not src.is_symlink():
            # if plain file or symlink (which links to file or directory)
            if dst_path.is_file() or dst_path.is_symlink():
                logger.info(f"{src}: {dst_path} exists but is not a directory")
                dst_path.unlink()
            if dst_path.is_dir():  # dst_path exists as a directory
                return  # keep it untouched
            logger.info(f"creating directory {dst_path}")
            dst_path.mkdir()
            self._copy_stat(src, dst_path)
        # src is plain file or symlink
        elif src.is_file() or src.is_symlink():  # includes broken symlink
            if dst_path.is_symlink() or dst_path.is_file():
                # When source is symlink, shutil.copy2 fails to overwrite destination file.
                # When destination is symlink, shutil.copy2 copy src file under destination.
                # To avoid these cases, remove destination file beforehand.
                dst_path.unlink()
            if dst_path.is_dir():
                logger.info(f"{src}: {dst_path} exists as a directory")
                shutil.rmtree(dst_path, ignore_errors=True)

            logger.info(f"copying file {dst_dir / src.name}")
            shutil.copy2(src, dst_path, follow_symlinks=False)
            self._copy_stat(src, dst_path)
        else:
            raise OtaErrorUnrecoverable(f"{src} unintended file type")

    def _copy_recursive(self, src: Path, dst_dir: Path):
        self._copy_preserve(src, dst_dir)
        if src.is_dir() and not src.is_symlink():
            for src_child in src.glob("*"):
                self._copy_recursive(src_child, dst_dir / src.name)
