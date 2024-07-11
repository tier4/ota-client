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


from __future__ import annotations

import logging
import os
import shutil
from functools import lru_cache, partial
from pathlib import Path

from otaclient_common.linux import (
    ParsedGroup,
    ParsedPasswd,
    map_gid_by_grpnam,
    map_uid_by_pwnam,
)

logger = logging.getLogger(__name__)


class PersistFilesHandler:
    """Preserving files in persist list from <src_root> to <dst_root>.

    Files being copied will have mode bits preserved,
    and uid/gid preserved with mapping as follow:

        src_uid -> src_name -> dst_name -> dst_uid
        src_gid -> src_name -> dst_name -> dst_gid
    """

    def __init__(
        self,
        src_passwd_file: str | Path,
        src_group_file: str | Path,
        dst_passwd_file: str | Path,
        dst_group_file: str | Path,
        *,
        src_root: str | Path,
        dst_root: str | Path,
    ):
        self._uid_mapper = lru_cache()(
            partial(
                self.map_uid_by_pwnam,
                src_db=ParsedPasswd(src_passwd_file),
                dst_db=ParsedPasswd(dst_passwd_file),
            )
        )
        self._gid_mapper = lru_cache()(
            partial(
                self.map_gid_by_grpnam,
                src_db=ParsedGroup(src_group_file),
                dst_db=ParsedGroup(dst_group_file),
            )
        )
        self._src_root = Path(src_root)
        self._dst_root = Path(dst_root)

    @staticmethod
    def map_uid_by_pwnam(
        *, src_db: ParsedPasswd, dst_db: ParsedPasswd, uid: int
    ) -> int:
        _mapped_uid = map_uid_by_pwnam(src_db=src_db, dst_db=dst_db, uid=uid)
        _usern = src_db._by_uid[uid]

        logger.info(f"{_usern=}: mapping src_{uid=} to {_mapped_uid=}")
        return _mapped_uid

    @staticmethod
    def map_gid_by_grpnam(*, src_db: ParsedGroup, dst_db: ParsedGroup, gid: int) -> int:
        _mapped_gid = map_gid_by_grpnam(src_db=src_db, dst_db=dst_db, gid=gid)
        _groupn = src_db._by_gid[gid]

        logger.info(f"{_groupn=}: mapping src_{gid=} to {_mapped_gid=}")
        return _mapped_gid

    def _chown_with_mapping(
        self, _src_stat: os.stat_result, _dst_path: str | Path
    ) -> None:
        _src_uid, _src_gid = _src_stat.st_uid, _src_stat.st_gid
        try:
            _dst_uid = self._uid_mapper(uid=_src_uid)
        except ValueError:
            logger.warning(f"failed to find mapping for {_src_uid=}, keep unchanged")
            _dst_uid = _src_uid

        try:
            _dst_gid = self._gid_mapper(gid=_src_gid)
        except ValueError:
            logger.warning(f"failed to find mapping for {_src_gid=}, keep unchanged")
            _dst_gid = _src_gid
        os.chown(_dst_path, uid=_dst_uid, gid=_dst_gid, follow_symlinks=False)

    @staticmethod
    def _rm_target(_target: Path) -> None:
        """Remove target with proper methods."""
        if not _target.exists():
            return
        if _target.is_symlink() or _target.is_file():
            return _target.unlink(missing_ok=True)
        if _target.is_dir():
            return shutil.rmtree(_target, ignore_errors=True)

        raise ValueError(f"{_target} is not normal file/symlink/dir, failed to remove")

    def _prepare_symlink(self, _src_path: Path, _dst_path: Path) -> None:
        _dst_path.symlink_to(os.readlink(_src_path))
        # NOTE: to get stat from symlink, using os.stat with follow_symlinks=False
        self._chown_with_mapping(os.stat(_src_path, follow_symlinks=False), _dst_path)

    def _prepare_dir(self, _src_path: Path, _dst_path: Path) -> None:
        _dst_path.mkdir(exist_ok=True)

        _src_stat = os.stat(_src_path, follow_symlinks=False)
        os.chmod(_dst_path, _src_stat.st_mode)
        self._chown_with_mapping(_src_stat, _dst_path)

    def _prepare_file(self, _src_path: Path, _dst_path: Path) -> None:
        shutil.copy(_src_path, _dst_path, follow_symlinks=False)

        _src_stat = os.stat(_src_path, follow_symlinks=False)
        os.chmod(_dst_path, _src_stat.st_mode)
        self._chown_with_mapping(_src_stat, _dst_path)

    def _prepare_parent(self, _path_relative_to_root: Path) -> None:
        """
        NOTE that we intensionally keep the already there parents' permission
            setting on destination.
        """
        for _parent in reversed(_path_relative_to_root.parents):
            _src_parent, _dst_parent = (
                self._src_root / _parent,
                self._dst_root / _parent,
            )
            if _dst_parent.is_dir():  # keep the origin parent on dst as it
                continue

            if _dst_parent.is_symlink() or _dst_parent.is_file():
                _dst_parent.unlink(missing_ok=True)
                self._prepare_dir(_src_parent, _dst_parent)
                continue

            if not _dst_parent.exists():
                self._prepare_dir(_src_parent, _dst_parent)
                continue

            raise ValueError(
                f"{_dst_parent=} is not a normal file/symlink/dir, cannot cleanup"
            )

    def _recursively_prepare_dir(self, src_path: Path, *, path_relative_to_root: Path):
        """Dive into src_dir and preserve everything under the src dir."""
        self._prepare_parent(path_relative_to_root)
        for src_curdir, dnames, fnames in os.walk(src_path, followlinks=False):
            src_cur_dpath = Path(src_curdir)
            dst_cur_dpath = self._dst_root / src_cur_dpath.relative_to(self._src_root)

            # ------ prepare current dir itself ------ #
            self._rm_target(dst_cur_dpath)
            self._prepare_dir(src_cur_dpath, dst_cur_dpath)

            # ------ prepare entries in current dir ------ #
            for _fname in fnames:
                _src_fpath, _dst_fpath = src_cur_dpath / _fname, dst_cur_dpath / _fname
                self._rm_target(_dst_fpath)
                if _src_fpath.is_symlink():
                    self._prepare_symlink(_src_fpath, _dst_fpath)
                    continue
                self._prepare_file(_src_fpath, _dst_fpath)

            # symlinks to dirs also included in dnames, we must handle it
            for _dname in dnames:
                _src_dpath, _dst_dpath = src_cur_dpath / _dname, dst_cur_dpath / _dname
                if _src_dpath.is_symlink():
                    self._rm_target(_dst_dpath)
                    self._prepare_symlink(_src_dpath, _dst_dpath)

    # API

    def preserve_persist_entry(self, _persist_entry: str | Path):
        """Preserve <_persist_entry> from active slot to standby slot.

        Args:
            _persist_entry (str | Path): The canonical path of the entry to be preserved.

        Raises:
            ValueError: Raised when src <_persist_entry> is not a regular file, symlink or directory,
                or failed to prepare destination.
        """
        # persist_entry in persists.txt must be rooted at /
        path_relative_to_root = Path(_persist_entry).relative_to("/")
        src_path = self._src_root / path_relative_to_root
        dst_path = self._dst_root / path_relative_to_root

        # ------ src is symlink ------ #
        # NOTE: always check if symlink first as is_file/is_dir/exists all follow_symlinks
        if src_path.is_symlink():
            logger.info(
                f"preserving symlink: {_persist_entry}, points to {os.readlink(src_path)}"
            )
            self._rm_target(dst_path)
            self._prepare_parent(path_relative_to_root)
            self._prepare_symlink(src_path, dst_path)
            return

        # ------ src is file ------ #
        if src_path.is_file():
            logger.info(f"preserving normal file: {_persist_entry}")
            self._rm_target(dst_path)
            self._prepare_parent(path_relative_to_root)
            self._prepare_file(src_path, dst_path)
            return

        # ------ src is dir ------ #
        if src_path.is_dir():
            logger.info(f"recursively preserve directory: {src_path}")
            self._recursively_prepare_dir(
                src_path, path_relative_to_root=path_relative_to_root
            )
            return

        # ------ src is not regular file/symlink/dir or missing ------ #
        _err_msg = f"{src_path=} doesn't exist"
        if src_path.exists():
            _err_msg = f"src must be either a file/symlink/dir, skip {src_path=}"

        logger.warning(_err_msg)
        raise ValueError(_err_msg)
