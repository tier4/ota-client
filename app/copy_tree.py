import os
import stat
import shutil
from pathlib import Path

import log_util
from configs import Config as cfg
from ota_error import OtaErrorUnrecoverable

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class CopyTree:
    def __init__(
        self,
        src_passwd_file: Path,
        src_group_file: Path,
        dst_passwd_file: Path,
        dst_group_file: Path,
    ):
        self._src_passwd = self._id_to_name_dict(src_passwd_file)
        self._src_group = self._id_to_name_dict(src_group_file)
        self._dst_passwd = self._name_to_id_dict(dst_passwd_file)
        self._dst_group = self._name_to_id_dict(dst_group_file)

    def copy_with_parents(self, src: Path, dst_dir: Path):
        dst_path = dst_dir
        if not dst_path.is_dir() or dst_path.is_symlink():
            raise OtaErrorUnrecoverable(f"{dst_path} should be plain directory")

        for parent in reversed(list(src.parents)):
            self._copy_preserve(parent, dst_path)
            dst_path = dst_path / parent.name
        self._copy_recursive(src, dst_path)

    """ private functions from here """

    def _id_to_name_dict(self, passwd_or_group_file: Path):
        lines = open(passwd_or_group_file).readlines()
        entries = {}
        for line in lines:
            e = line.split(":")
            entries[e[2]] = e[0]  # {uid: name}
        return entries

    def _name_to_id_dict(self, passwd_or_group_file: Path):
        lines = open(passwd_or_group_file).readlines()
        entries = {}
        for line in lines:
            e = line.split(":")
            entries[e[0]] = e[2]  # {name: uid}
        return entries

    # src_uid -> src_name -> dst_name -> dst_uid
    # src_gid -> src_name -> dst_name -> dst_gid
    def _convert_src_id_to_dst_id(self, src_uid, src_gid):
        logger.info(f"src_uid: {src_uid}, src_gid: {src_gid}")
        user = self._src_passwd[str(src_uid)]
        uid = self._dst_passwd[user]
        group = self._src_group[str(src_gid)]
        gid = self._dst_group[group]
        logger.info(f"dst_uid: {uid}, dst_gid: {gid}")
        return int(uid), int(gid)

    def _copy_stat(self, src, dst):
        st = os.stat(src, follow_symlinks=False)
        try:
            dst_uid, dst_gid = self._convert_src_id_to_dst_id(
                st[stat.ST_UID], st[stat.ST_GID]
            )
        except KeyError:  # In case src UID/GID not found, keep UID/GID as is.
            logger.warning(f"uid: {st[stat.ST_UID]}, gid: {st[stat.ST_GID]} not found")
            dst_uid, dst_gid = st[stat.ST_UID], st[stat.ST_GID]
        os.chown(dst, dst_uid, dst_gid, follow_symlinks=False)
        if not dst.is_symlink():  # symlink always 777
            os.chmod(dst, st[stat.ST_MODE])

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
                shutil.rmtree(dst_path)

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
