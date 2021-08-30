#!/usr/bin/env python3

import sys
import tempfile
import os
import stat
import shlex
import shutil
import subprocess
import urllib.parse
import requests
import yaml
import logging
import copy
import re
import time
from pathlib import Path
from typing import List
from multiprocessing import Pool, Manager
from hashlib import sha256


import configs as cfg
import constants
from ota_status import OtaStatus
from constants import OtaStatusString
from grub_control import GrubCtl
from ota_metadata import OtaMetaData
from exceptions import Error, OtaError

from logging import getLogger, INFO, DEBUG

logger = getLogger(__name__)
logger.setLevel(cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL))


def _file_sha256(filename) -> str:
    with open(filename, "rb") as f:
        return sha256(f.read()).hexdigest()


def _copy_complete(src_file: Path, dst_file: Path):
    """
    copy file complete
    """
    src_dir = src_file.parent
    dst_dir = dst_file.parent
    _copydirs_complete(src_dir, dst_dir)
    shutil.copy2(src_file, dst_file, follow_symlinks=False)
    # copy owner and group
    st = os.stat(src_file, follow_symlinks=False)
    os.chown(dst_file, st[stat.ST_UID], st[stat.ST_GID], follow_symlinks=False)


def _copydirs_complete(src: Path, dst: Path):
    """
    copy directory path complete
    """
    if dst.is_dir():
        # directory exist
        return True
    # check parent directory
    src_parent_dir = src.parent
    dst_parent_dir = dst.parent
    if dst_parent_dir.is_dir() or _copydirs_complete(src_parent_dir, dst_parent_dir):
        # parent exist, make directory
        logger.debug(f"mkdir: {dst}")
        dst.mkdir()
        shutil.copystat(src, dst, follow_symlinks=False)
        st = os.stat(src, follow_symlinks=False)
        os.chown(dst, st[stat.ST_UID], st[stat.ST_GID])
        return True
    return False


def _copytree_complete(src: Path, dst: Path) -> Path:
    """
    directory complete copy from src directory to dst directory.
    dst directory should not exist.
    """
    # make directory on the destination
    _copydirs_complete(src, dst)
    # get directory entories
    with os.scandir(src) as itr:
        entries = list(itr)
    errors = []
    # copy entries
    for srcentry in entries:
        srcname = src.joinpath(srcentry.name)
        dstname = dst.joinpath(srcentry.name)
        try:
            if srcentry.is_symlink():
                linksrc = Path(os.readlink(srcname))
                dstname.symlink_to(linksrc)
                st = os.stat(srcname, follow_symlinks=False)
                os.chown(
                    dstname, st[stat.ST_UID], st[stat.ST_GID], follow_symlinks=False
                )
            elif srcentry.is_dir():
                _copytree_complete(srcname, dstname)
            else:
                _copy_complete(srcname, dstname)
        except Error as e:
            errors.extend(e.args[0])
        except OSError as why:
            errors.append((srcname, dstname, str(why)))
    if errors:
        raise Error(errors)
    return dst


def _read_ecuid(ecuid_file: Path) -> str:
    """
    initial read ECU ID
    """
    logger.debug(f"ECU ID file: {ecuid_file}")
    with open(ecuid_file, mode="r") as f:
        ecuid = f.readline().replace("\n", "")
        logger.debug(f"line: {ecuid}")
    return ecuid


def _read_ecu_info(ecu_info_yaml_file: Path):
    """
    Read ECU Information from yaml file.
    """
    with open(ecu_info_yaml_file, "r") as fyml:
        logger.debug(f"open: {ecu_info_yaml_file}")
        ecuinfo = yaml.load(fyml, Loader=yaml.SafeLoader)
    return ecuinfo


def _mount_bank(bank: Path, target_dir: Path):
    """
    mount next bank
    """
    command_line = f"mount {bank} {target_dir}"
    logger.debug(f"commandline: {command_line}")
    subprocess.check_output(shlex.split(command_line))


def _unmount_bank(target_dir: Path):
    """
    unmount bank
    """
    if target_dir.is_mount():
        command_line = f"umount {target_dir}"
        logger.debug(f"commandline: {command_line}")
        subprocess.check_output(shlex.split(command_line))


def _cleanup_dir(target_dir: Path):
    """
    cleanup next bank
    """
    logger.debug(f"cleanup directory: {target_dir}")
    command_line = f"rm -rf {str(target_dir)}/*"
    logger.debug(f"commandline: {command_line}")
    proc = subprocess.call(command_line, shell=True)
    if proc != 0:
        raise Exception(f"{command_line} : return {proc}")


def _gen_directories(dirlist_file: Path, target_dir: Path):
    """
    generate directories on another bank
    """
    with open(dirlist_file) as f:
        for l in f.read().splitlines():
            dirinf = DirectoryInf(l)
            logger.debug(f"dir inf: {dirinf.path}")
            target_path = target_dir.joinpath(dirinf.path.relative_to("/"))
            logger.debug(f"target path: {target_path}")
            target_path.mkdir(mode=dirinf.mode, parents=True, exist_ok=True)
            os.chown(target_path, dirinf.uid, dirinf.gid)
            os.chmod(target_path, dirinf.mode)
    return True


def _copy_persistent(src_path: Path, target_dir: Path):
    """
    copy persistent dir/file
    """
    if src_path.root == "/":
        dest_path = target_dir.joinpath(src_path.relative_to("/"))
    else:
        dest_path = target_dir.joinpath(src_path)

    if src_path.exists():
        if src_path.is_dir():
            if dest_path.exists():
                logger.debug(f"rmtree: {dest_path}")
                shutil.rmtree(dest_path)
            logger.debug(f"persistent dir copy: {src_path} -> {dest_path}")
            _copytree_complete(src_path, dest_path)
        else:
            # target path points to a file
            logger.debug(f"persistent file copy: {src_path} -> {dest_path}")
            if dest_path.exists():
                logger.info(f"rm file: {dest_path}")
                dest_path.unlink(missing_ok=True)
            _copy_complete(src_path, dest_path)
    else:
        logger.warning(f"persistent file not exist: {src_path}")


def _gen_persistent_files(list_file: Path, target_dir: Path):
    """
    generate persistent files
    """
    with open(list_file, mode="r") as f:
        for l in f.read().splitlines():
            persistent_info = PersistentInf(l)
            src_path = persistent_info.path
            if Path("/boot") in src_path.parents:
                # /boot directory
                logger.info(f"do nothing for boot dir file: {src_path}")
            else:
                # others
                _copy_persistent(src_path, target_dir)


def _header_str_to_dict(header_str):
    """"""
    header_dict = {}
    for l in header_str.split(","):
        kl = l.split(":")
        if len(kl) == 2:
            header_dict[kl[0]] = kl[1]
    return header_dict


def _save_update_ecuinfo(update_ecuinfo_yaml_file: Path, update_ecu_info: Path):
    """
    save update ecuinfo.yaml
    """
    output_file = update_ecuinfo_yaml_file
    logger.info(f"output_file: {output_file}")
    with tempfile.NamedTemporaryFile("w", delete=False, prefix=__name__) as ftmp:
        tmp_file_name = ftmp.name
        ftmp.write(yaml.dump(update_ecu_info))

    shutil.move(tmp_file_name, output_file)
    return True


class _BaseInf:
    _base_pattern = re.compile(
        r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+),(?P<left_over>.*)"
    )

    def __init__(self, info: str):
        match_res: re.Match = self._base_pattern.match(info.strip("\n"))
        assert match_res is not None
        self.mode = int(match_res.group("mode"), 8)
        self.uid = int(match_res.group("uid"))
        self.gid = int(match_res.group("gid"))

        self._left: str = match_res.group("left_over")


class DirectoryInf(_BaseInf):
    """
    Directory file information class
    """

    def __init__(self, info):
        super().__init__(info)
        self.path = Path(self._left[1:-1])


class SymbolicLinkInf(_BaseInf):
    """
    Symbolik link information class
    """

    _pattern = re.compile(r"'(?P<link>.+)','(?P<target>.+)'")

    def __init__(self, info):
        super().__init__(info)
        res = self._pattern.match(self._left)
        assert res is not None
        self.slink = Path(res.group("link"))
        self.srcpath = Path(res.group("target"))


class RegularInf(_BaseInf):
    """
    Regular file information class
    """

    _pattern = re.compile(r"(?P<nlink>\d+),(?P<hash>\w+),'(?P<path>.+)'")

    def __init__(self, info):
        super().__init__(info)

        res = self._pattern.match(self._left)
        assert res is not None
        self.nlink = int(res.group("nlink"))
        self.sha256hash = res.group("hash")
        self.path = Path(res.group("path"))


class PersistentInf(_BaseInf):
    """
    Persistent file information class
    """

    def __init__(self, info: str):
        self.path = Path(info[1:-1])


class OtaCache:
    def __init__(self, directory=cfg.OTA_CACHE_DIR):
        self._directory = Path(directory)
        directory.mkdir(exist_ok=True)

    def save(self, name):
        dst: Path = self._directory / name
        dst.parent.mkdir(exist_ok=True)
        _copy_complete(name, dst)

    def restore(self, name, target_hash):
        src: Path = self._directory / name
        if src.is_file() and _file_sha256(src) == target_hash:
            _copy_complete(src, name)
            return True
        return False


class OtaClient:
    """
    OTA Client class
    """

    #
    # files
    #
    ecuid_file = cfg.ECUID_FILE
    ecuinfo_yaml_file = cfg.ECUINFO_YAML_FILE
    _grub_conf_file = cfg.GRUB_CFG_FILE
    _fstab_file = cfg.FSTAB_FILE

    #
    # dirs
    #
    _ota_dir = cfg.OTA_DIR
    _mount_point = cfg.MOUNT_POINT
    _grub_dir = cfg.GRUB_DIR
    _rollback_dir = cfg.ROLLBACK_DIR

    def __init__(
        self,
        boot_status=constants.OtaBootStatusString.NORMAL_BOOT,
        url="",
        ota_cache=None,
    ):
        """
        OTA Client initialize
        """
        self.__main_ecu = True
        # OTA
        self.boot_status = boot_status
        self._ota_status = OtaStatus()
        self._grub_ctl = GrubCtl()
        self._boot_vmlinuz = None
        self._boot_initrd = None
        # ECU information
        self.__my_ecuid = _read_ecuid(self.ecuid_file)
        self.__ecuinfo_yaml_file = self.ecuinfo_yaml_file
        self.__ecu_info = _read_ecu_info(self.ecuinfo_yaml_file)
        self.__update_ecuinfo_yaml_file = self.__ecuinfo_yaml_file.with_suffix(
            self.__ecuinfo_yaml_file.suffix + ".update"
        )
        self.__update_ecu_info = copy.deepcopy(self.__ecu_info)
        # remote
        self.__url = url
        self.__header_dict = {}
        self.__download_retry = 5
        self._ota_cache = ota_cache
        # metadata data
        self._metadata = None

        if not self._mount_point.is_dir():
            self._mount_point.mkdir(parents=True, exist_ok=True)

        # rollback info
        self._rollback_dict = {}
        self.backup_files = {
            "dirlist": "dirlist.txt",
            "symlinklist": "symlinklist.txt",
            "regularlist": "regularlist.txt",
            "persistentlist": "persistentlist.txt",
        }

    def is_main_ecu(self):
        return self.__main_ecu

    def get_my_ecuid(self):
        return self.__my_ecuid

    def get_ecuinfo(self):
        """"""
        return self.__ecu_info

    def get_boot_status(self):
        return self.boot_status

    def get_ota_status(self):
        return self._ota_status.get_ota_status()

    def _set_url(self, url):
        self.__url = url

    def _get_metadata_url(self):
        """
        get metadata URL
        """
        return urllib.parse.urljoin(self.__url, "metadata.jwt")

    def _download_raw(self, url, target_file):
        """"""
        header = self.__header_dict  # self.__cookie
        header["Accept-encording"] = "gzip"
        response = requests.get(url, headers=header, timeout=10)
        if response.status_code != 200:
            logger.error(f"status_code={response.status_code}, url={url}")
            return response, ""

        m = sha256()
        total_length = response.headers.get("content-length")
        if total_length is None:
            m.update(response.content)
            target_file.write(response.content)
        else:
            dl = 0
            total_length = int(total_length)
            for data in response.iter_content(chunk_size=4096):
                dl += len(data)
                m.update(data)
                target_file.write(data)
                # TODO: not sure what these codes are for
                # NOTE: CRITICAL:50, ERROR:40, WARNING:30, INFO:20, DEBUG:10
                if logging.root.level <= logging.DEBUG:
                    done = int(50 * dl / total_length)
                    sys.stdout.write("\r[%s%s]" % ("=" * done, " " * (50 - done)))
                    sys.stdout.flush()
        return response, m.hexdigest()

    def _download(self, url):
        """"""
        header = self.__header_dict  # self.__cookie
        header["Accept-encording"] = "gzip"
        response = requests.get(url, headers=header)
        response.encoding = response.apparent_encoding
        logger.debug(f"encording: {response.encoding}")
        # logger.info(response.text)
        return response

    def _download_raw_file(self, url, dest_file: Path, target_hash=""):
        """
        download file
        """
        logger.debug(f"DL File: {dest_file}")
        digest = ""
        time_stamp = str(time.time())
        try:
            with tempfile.NamedTemporaryFile(
                "wb", delete=False, prefix=__name__ + time_stamp
            ) as ftmp:
                tmp_file_name = ftmp.name
                # download
                response, digest = self._download_raw(url, ftmp)
                if response.status_code != 200:
                    logger.error(f"status_code={response.status_code}, url={url}")
                    return False

            # file move
            shutil.move(tmp_file_name, dest_file)
        except Exception as e:
            logger.exception(f"File download error!: {e}")
            return False
        # check sha256 hash
        if target_hash != "" and digest != target_hash:
            logger.error(f"hash missmatch: {dest_file}")
            logger.error(f"  dl hash: {digest}")
            logger.error(f"  hash: {target_hash}")
            return False
        return True

    def _download_raw_file_with_retry(self, url, dest_file: Path, target_hash=""):
        """"""
        for i in range(self.__download_retry):
            if self._download_raw_file(url, dest_file, target_hash):
                logger.debug(f"retry count: {i}")
                return True
        return False

    def _download_metadata_jwt(self, metadata_url):
        """
        Download metadata.jwt
        """
        # url = self._get_metadata_url()
        return self._download(metadata_url)

    def _download_metadata_jwt_file(self, dest_file: Path):
        """
        Download metadata.jwt file(for debagging)
        """
        try:
            # download metadata.jwt
            metadata_url = self._get_metadata_url()
            dest_path = cfg.TMP_DIR.joinpath(dest_file)
            logger.debug(f"url: {metadata_url}")
            logger.debug(f"metadata dest path: {dest_path}")
            if not self._download_raw_file_with_retry(metadata_url, dest_path):
                return False
        except Exception as e:
            logger.exception("metadata error!:")
            return False
        return True

    def _download_metadata(self, metadata_file: Path, metadata_url):
        """
        Download OTA metadata
        """
        try:
            # dounload and write meta data.
            # url = self._get_metadata_url()
            logger.debug(f"metadata url: {metadata_url}")
            response = self._download(metadata_url)
            logger.debug(f"response: {response.status_code}")
            if response.status_code == 200:
                self._metadata_jwt = response.text
                metadata_file.write_text(response.text)
                self._metadata = OtaMetaData(self._metadata_jwt)
            else:
                self._metadata_jwt = ""
        except Exception as e:
            self._meta_data_file = ""
            logger.exception("Error: OTA meta data download fail.:")
            return False
        return True

    def _download_certificate(self, cert_file):
        """
        Download certificate file
        """
        url = urllib.parse.urljoin(self.__url, cert_file)
        return self._download(url)

    def _verify_metadata_jwt(self, metadata: OtaMetaData):
        """
        verify metadata.jwt
        """
        cert = metadata.get_certificate_info()
        response = self._download_certificate(cert["file"])
        if response.status_code == 200:
            pem = response.text
            hash = cert["hash"]
            if sha256(pem.encode()).hexdigest() != hash:
                logger.error("certificate hash missmatch:")
                logger.error(f"    dl hash: {sha256(pem).hexdigest()}")
                logger.error(f"    hash: {hash}")
                return False
            return metadata.verify(pem)
        else:
            logger.error(f"response error: {response.status_code}")
            return False

    def _cleanup_rollback_dir(self):
        """
        clean up rollback directory
        """
        if self._rollback_dir.is_dir():
            logger.info(f"removedir: {self._rollback_dir}")
            shutil.rmtree(self._rollback_dir)
        logger.debug(f"makedir: {self._rollback_dir}")
        self._rollback_dir.mkdir(exist_ok=True)

    def _prepare_next_bank(self, bank: Path, target_dir: Path):
        """
        prepare next boot bank
            mount & clean up
        """
        try:
            # unmount
            _unmount_bank(target_dir)
            # mount
            _mount_bank(bank, target_dir)
            # cleanup
            _cleanup_dir(target_dir)
            # clean rollback dir
            self._cleanup_rollback_dir()
        except:
            logger.exception("Standby bank preoparing error!")
            _unmount_bank(target_dir)
            return False
        return True

    def _download_list_file(self, url, list_file: Path, hash=""):
        """
        Download list file(debug)
        """
        dirs_url = urllib.parse.urljoin(url, list_file)
        dest_path = cfg.TMP_DIR.joinpath(list_file)
        return self._download_raw_file_with_retry(dirs_url, dest_path, hash)

    def _setup_directories(self, target_dir: Path):
        """
        generate directories on another bank
        """
        # get directories metadata
        dirs = self._metadata.get_directories_info()
        dirs_url = urllib.parse.urljoin(self.__url, dirs["file"])
        tmp_list_file = cfg.TMP_DIR.joinpath(dirs["file"])
        if self._download_raw_file_with_retry(dirs_url, tmp_list_file, dirs["hash"]):
            # generate directories
            if _gen_directories(tmp_list_file, target_dir):
                # move list file to rollback dir
                dest_file = self._rollback_dir / self.backup_files["dirlist"]
                shutil.move(tmp_list_file, dest_file)
                return True
        return False

    def _gen_symbolic_links(self, symlinks_file: Path, target_dir: Path):
        """
        generate symbolic_links on another bank
        """
        res = True
        with open(symlinks_file, mode="r") as f:
            try:
                for l in f.read().splitlines():
                    slinkf = SymbolicLinkInf(l)
                    logger.debug(f"src: {slinkf.srcpath}")
                    logger.debug(f"slink: {slinkf.slink}")
                    if Path("/boot") in slinkf.slink.parents == 0:
                        # /boot directory
                        try:
                            dest_file = ""
                            if slinkf.slink.is_symlink():
                                dest_dir = self._rollback_dir
                                shutil.move(slinkf.slink, dest_dir)
                            slinkf.slink.symlink_to(slinkf.srcpath)
                            os.chown(
                                slinkf.slink,
                                slinkf.uid,
                                slinkf.gid,
                                follow_symlinks=False,
                            )
                        except Exception as e:
                            logger.exception("symbolic link error!")
                            if dest_file != "":
                                shutil.move(dest_file, slinkf.slink)
                            raise (OtaError("Cannot make symbolic link."))
                    else:
                        # others
                        slink = target_dir.joinpath(slinkf.slink.relative_to("/"))
                        slink.symlink_to(slinkf.srcpath)
                        os.chown(
                            slink,
                            slinkf.uid,
                            slinkf.gid,
                            follow_symlinks=False,
                        )
            except Exception as e:
                logger.exception("symboliclink error:")
                res = False
        return res

    def _setup_symboliclinks(self, target_dir: Path):
        """
        generate symboliclinks on another bank
        """
        # get symboliclink metadata
        symlinks = self._metadata.get_symboliclinks_info()
        symlinks_url = urllib.parse.urljoin(self.__url, symlinks["file"])
        tmp_list_file = cfg.TMP_DIR.joinpath(symlinks["file"])
        if self._download_raw_file_with_retry(
            symlinks_url, tmp_list_file, symlinks["hash"]
        ):
            # generate symboliclinks
            if self._gen_symbolic_links(tmp_list_file, target_dir):
                # move list file to rollback dir
                dest_file = self._rollback_dir.joinpath(
                    self.backup_files["symlinklist"]
                )
                shutil.move(tmp_list_file, dest_file)
                return True
        return False

    def _download_regular_file(
        self, rootfs_dir: str, target_path: Path, regular_file: Path, hash256
    ):
        """
        Download regular file
        """
        # download new file
        regular_url = urllib.parse.urljoin(
            self.__url,
            urllib.parse.quote(
                str(Path(rootfs_dir).joinpath(regular_file.relative_to("/")))
            ),
        )
        logger.debug(f"download file: {regular_url}")
        return self._download_raw_file_with_retry(regular_url, target_path, hash256)

    def _gen_boot_dir_file(
        self,
        rootfs_dir: str,
        target_dir: Path,
        regular_inf: RegularInf,
        prev_inf: RegularInf,
    ):
        """
        generate /boot directory file
        """
        staging_kernel_files = global_var_dict["staging-_kernel_files"]
        # starts with `/boot/vmlinuz-`.
        match = re.match(r"^/boot/(vmlinuz-.*)", str(regular_inf.path))
        if match is not None:
            staging_kernel_files["vmlinuz"] = match.group(1)

        # starts with `/boot/initrd.img-`, but doesnot end with `.old-dkms`.
        match = re.match(
            r"^(?!.*\.old-dkms$)/boot/(initrd\.img-.*)", str(regular_inf.path)
        )
        if match is not None:
            staging_kernel_files["initrd"] = match.group(1)

        if prev_inf and prev_inf.sha256hash == regular_inf.sha256hash:
            # create hard link
            logger.debug(f"links: {regular_inf.nlink}")
            prev_inf.path.link_to(regular_inf.path)
        else:
            staging_rollback_dict = global_var_dict["staging-dict-_rollback_dict"]
            # no hard link
            if (
                regular_inf.path.is_file()
                and not regular_inf.path.is_symlink()
                and _file_sha256(regular_inf.path) == regular_inf.sha256hash
            ):
                # nothing to do
                rollback_file = self._rollback_dir.joinpath(regular_inf.path.name)
                _copy_complete(regular_inf.path, rollback_file)

                staging_rollback_dict[regular_inf.path] = regular_inf.path
                logger.debug("file already exist! no copy or download!")
            else:
                if regular_inf.path.is_file():
                    # backup for rollback
                    rollback_file = self._rollback_dir.joinpath(regular_inf.path.name)
                    _copy_complete(regular_inf.path, rollback_file)
                    staging_rollback_dict[regular_inf.path] = rollback_file
                else:
                    staging_rollback_dict[regular_inf.path] = ""
                # download new file
                if self._download_regular_file(
                    rootfs_dir,
                    regular_inf.path,
                    regular_inf.path,
                    regular_inf.sha256hash,
                ):
                    logger.debug(f"Download: {regular_inf.path}")
                    logger.debug(f"file hash: {regular_inf.sha256hash}")
                else:
                    raise OtaError("Filedown load error! {regular_inf}")
                logger.debug(f"regular_file: {regular_inf.path}")
                os.chown(regular_inf.path, regular_inf.uid, regular_inf.gid)
                os.chmod(regular_inf.path, regular_inf.mode)

    def _gen_regular_file(
        self,
        rootfs_dir: str,
        target_dir: Path,
        regular_inf: RegularInf,
        prev_inf: RegularInf,
    ):
        """
        generate regular file
        """
        dest_path = target_dir.joinpath(regular_inf.path.relative_to("/"))
        if prev_inf and prev_inf.sha256hash == regular_inf.sha256hash:
            # create hard link
            logger.debug(f"links: {regular_inf.nlink}")
            src_path = target_dir.joinpath(prev_inf.path.relative_to("/"))
            src_path.link_to(dest_path)
        else:
            # no hard link
            logger.debug(f"No hard links: {regular_inf.nlink}")
            current_file: Path = regular_inf.path
            if (
                current_file.is_file()
                and not current_file.is_symlink()
                and _file_sha256(current_file) == regular_inf.sha256hash
            ):
                # copy from current bank
                logger.debug(f"copy from current: {current_file}")
                _copy_complete(current_file, dest_path)
            else:
                # use ota cache if available
                if self._ota_cache is not None and self._ota_cache.restore(
                    dest_path, regular_inf.sha256hash
                ):
                    # TODO: NOT IMPLEMENTED
                    pass
                # download new file
                elif self._download_regular_file(
                    rootfs_dir, dest_path, regular_inf.path, regular_inf.sha256hash
                ):
                    if self._ota_cache is not None:
                        self._ota_cache.save(dest_path)
                    logger.debug(f"Download: {regular_inf.path}")
                    logger.debug(f"file hash: {regular_inf.sha256hash}")
                else:
                    raise OtaError("File download error! {regular_inf}")
            logger.debug(f"regular_file: {dest_path}")
            logger.debug(f"permissoin: {regular_inf.mode}")
            os.chown(dest_path, regular_inf.uid, regular_inf.gid)
            os.chmod(dest_path, regular_inf.mode)

    def _gen_regular_files(
        self, rootfs_dir: str, regulars_file: RegularInf, target_dir: Path
    ):
        """
        generate regular files
        """
        self._boot_vmlinuz = None  # clear
        self._boot_initrd = None  # clear

        cwd = os.getcwd()
        os.chdir(target_dir)

        rfiles_list = []
        with open(regulars_file) as f:
            logger.debug(f"target_dir: {target_dir}")
            rfiles_list = [RegularInf(l) for l in f.readlines()]

        # process all regular files here
        try:
            self._process_regular_files(rootfs_dir, rfiles_list, target_dir)
        except:
            logger.exception(f"gen regular files error!")
            return False
        finally:
            os.chdir(cwd)

        return True

    def _process_regular_files_pool_init(self, gvar_dict: dict, awc: list):
        """
        Used by _process_regular_files
        Init the worker pool with shared variables

        DO NOT call this method from other functions
        except for _process_regular_files
        """
        global global_var_dict, await_counter
        global_var_dict = gvar_dict
        await_counter = awc

    def _process_regular_files_exit(self, gvar_dict: dict):
        """
        Used by _process_regular_files
        Update corresponding class attributes

        DO NOT call this method from other functions
        except for _process_regular_files
        """
        setattr(self, "_rollback_dict", dict(gvar_dict["staging-dict-_rollback_dict"]))
        setattr(self, "_boot_vmlinuz", gvar_dict["staging-_kernel_files"]["vmlinuz"])
        setattr(self, "_boot_initrd", gvar_dict["staging-_kernel_files"]["initrd"])

    def _process_regular_files(
        self, rootfs_dir: str, rfiles_list: List[RegularInf], target_dir: Path
    ):
        with Manager() as manager:
            ecb_queue = manager.Queue()
            # variables passed to child processes
            # variable naming pattern: <prefix>-<type>-<var_name>
            # prefix tmp: for temporary use
            # prefix staging: used to update corresponding class attribute
            gvar_dict = {
                "tmp-dict-hardlink_reg": manager.dict(),
                "staging-dict-_rollback_dict": manager.dict(),
                "staging-_kernel_files": manager.dict(),
            }
            await_c = manager.list()

            # default to one worker per CPU core
            with Pool(
                initializer=self._process_regular_files_pool_init,
                initargs=(gvar_dict, await_c),
            ) as pool:

                # error_callback for workers
                #   signal the main process to terminate the pool
                def ecb(e):
                    ecb_queue.put(e)

                for rfile_inf in rfiles_list:
                    if (
                        rfile_inf.nlink >= 2
                        and rfile_inf.sha256hash
                        not in gvar_dict["tmp-dict-hardlink_reg"]
                    ):
                        # block the flow until the first copy of hardlinked file is ready
                        try:
                            pool.apply(
                                self._process_regular_file,
                                (rootfs_dir, target_dir, rfile_inf),
                            )
                        except Exception as e:
                            ecb(e)
                            break
                    else:
                        try:
                            pool.apply_async(
                                self._process_regular_file,
                                (rootfs_dir, target_dir, rfile_inf),
                                error_callback=ecb,
                            )
                        except:
                            # the only exception will be catched is ValueError
                            # caused by calling pool.apply_async when the pool terminated
                            pass

                # stop accepting new tasks
                pool.close()
                # wait for all tasks to complete
                # not apply timeout currently
                while len(await_c) < len(rfiles_list):
                    # if one of the subprocess raise error,
                    # terminate the whole pool
                    if not ecb_queue.empty():
                        pool.terminate()

                        logger.error(
                            f"process regular files failed. All sub processess terminated."
                        )
                        logger.error(f"last exception: {ecb_queue.get()}")
                        raise OtaError(f"process regular files failed!")

            # everything is ALLRIGHT!
            # update corresponding class attribute
            self._process_regular_files_exit(gvar_dict)

    def _process_regular_file(
        self, rootfs_dir: str, target_dir: Path, rfile_inf: RegularInf
    ):
        """
        main entry for paralleling processing regular files
        """
        try:
            prev_inf: RegularInf = None
            # hardlinked file
            if rfile_inf.nlink >= 2:
                prev_inf = global_var_dict["tmp-dict-hardlink_reg"].setdefault(
                    rfile_inf.sha256hash, rfile_inf
                )
                # if the upcoming rfile entry is the first copy of hardlinked file
                # then the prev_inf should be None
                if prev_inf.path == rfile_inf.path:
                    prev_inf = None

            if Path("/boot") in rfile_inf.path.parents:
                # /boot directory file
                logger.debug(f"boot file: {rfile_inf.path}")
                self._gen_boot_dir_file(rootfs_dir, target_dir, rfile_inf, prev_inf)
            else:
                # others
                logger.debug(f"no boot file: {rfile_inf.path}")
                self._gen_regular_file(rootfs_dir, target_dir, rfile_inf, prev_inf)
        except Exception as e:
            logger.exception(f"worker[{os.getpid()}]: process regular file failed!")
            raise e

        # if job finished successfully
        await_counter.append(True)

    def _setup_regular_files(self, target_dir: Path):
        """
        update files copy to another bank
        """
        rootfsdir_info = self._metadata.get_rootfsdir_info()
        # get regular metadata
        regularslist = self._metadata.get_regulars_info()
        regularslist_url = urllib.parse.urljoin(self.__url, regularslist["file"])
        tmp_list_file = cfg.TMP_DIR.joinpath(regularslist["file"])
        if self._download_raw_file_with_retry(
            regularslist_url, tmp_list_file, regularslist["hash"]
        ):
            if self._gen_regular_files(
                rootfsdir_info["file"], tmp_list_file, target_dir
            ):
                # move list file to rollback dir
                dest_file = self._rollback_dir.joinpath(
                    self.backup_files["regularlist"]
                )
                shutil.move(tmp_list_file, dest_file)
                return True
            if self._boot_vmlinuz is None or self._boot_initrd is None:
                logger.warning(
                    "vmlinuz or initrd is not set. This condition will be treated as an error in the future."
                )
        return False

    def _setup_persistent_files(self, target_dir: Path):
        """
        setup persistent files
        """
        # get persistent metadata
        persistent = self._metadata.get_persistent_info()
        persistent_url = urllib.parse.urljoin(self.__url, persistent["file"])
        tmp_list_file = cfg.TMP_DIR.joinpath(persistent["file"])
        if not self._download_raw_file_with_retry(
            persistent_url, tmp_list_file, persistent["hash"]
        ):
            logger.error(f"persistent file download error: {persistent_url}")
            return False
        else:
            logger.info(f"persistent file download success: {persistent_url}")

        # generate persistent files, copying from current bank.
        _gen_persistent_files(tmp_list_file, target_dir)
        # move list file to rollback dir
        dest_file = self._rollback_dir.joinpath(self.backup_files["persistentlist"])
        shutil.move(tmp_list_file, dest_file)
        return True

    def _setup_next_bank_fstab(self, fstab_file: Path, target_dir: Path):
        """
        setup next bank to fstab
        """
        if not fstab_file.is_file():
            logger.error(f"file not exist: {fstab_file}")
            return False

        dest_fstab_file = target_dir.joinpath(fstab_file.relative_to("/"))

        return self._grub_ctl.gen_next_bank_fstab(dest_fstab_file)

    def _construct_next_bank(self, next_bank: Path, target_dir: Path):
        """
        next bank construction
        """
        #
        # prepare next bank
        #
        if not self._prepare_next_bank(next_bank, target_dir):
            return False
        #
        # setup directories
        #
        if not self._setup_directories(target_dir):
            return False
        #
        # setup symbolic links
        #
        if not self._setup_symboliclinks(target_dir):
            return False
        #
        # setup regular files
        #
        if not self._setup_regular_files(target_dir):
            return False
        #
        # setup persistent file
        #
        if not self._setup_persistent_files(target_dir):
            return False
        #
        # setup fstab
        #
        if not self._setup_next_bank_fstab(self._fstab_file, target_dir):
            return False

        return True

    # TODO: move this function to grub_control?
    def _get_switch_status_for_reboot(self, next_bank: Path):
        """
        get switch status for reboot
        """
        if self._grub_ctl.is_banka(next_bank):
            return OtaStatusString.SWITCHA_STATE
        elif self._grub_ctl.is_bankb(next_bank):
            return OtaStatusString.SWITCHB_STATE
        raise Exception("Bank is not A/B bank!")

    def _inform_update_error(self, error):
        """
        inform update error
        """
        logger.error(f"Update error!: {str(error)}")
        # TODO : implement

        return

    def set_update_ecuinfo(self, update_info):
        """"""
        logger.info("_update_ecu_info start")
        ecuinfo = update_info.ecu_info
        logger.debug(f"[ecu_info] {ecuinfo}")
        ecu_found = False
        if ecuinfo.ecu_id == self.__update_ecu_info["main_ecu"]["ecu_id"]:
            logger.info("ecu_id matched!")
            self.__update_ecu_info["main_ecu"]["ecu_name"] = ecuinfo.ecu_name
            self.__update_ecu_info["main_ecu"]["ecu_type"] = ecuinfo.ecu_type
            self.__update_ecu_info["main_ecu"]["version"] = ecuinfo.version
            self.__update_ecu_info["main_ecu"]["independent"] = ecuinfo.independent
            ecu_found = True
            logger.debug(f"__update_ecu_info: {self.__update_ecu_info}")
        else:
            logger.debug("ecu_id not matched!")
            if "sub_ecus" in self.__update_ecu_info:
                for i, subecuinfo in enumerate(self.__update_ecu_info["sub_ecus"]):
                    ecuinfo = subecuinfo.ecu_info
                    if ecuinfo.ecu_id == subecuinfo["ecu_id"]:
                        self.__update_ecu_info["sub_ecus"][i][
                            "ecu_name"
                        ] = ecuinfo.ecu_name
                        self.__update_ecu_info["sub_ecus"][i][
                            "ecu_type"
                        ] = ecuinfo.ecu_type
                        self.__update_ecu_info["sub_ecus"][i][
                            "version"
                        ] = ecuinfo.version
                        self.__update_ecu_info["sub_ecus"][i][
                            "independent"
                        ] = ecuinfo.independent
                        ecu_found = True
        logger.info("_update_ecu_info end")
        return ecu_found

    def update(self, ecu_update_info):
        """
        OTA update execution
        """
        # -----------------------------------------------------------
        # set 'UPDATE' state
        self._ota_status.set_ota_status(OtaStatusString.UPDATE_STATE)
        logger.debug(ecu_update_info)
        self.__url = ecu_update_info.url
        metadata = ecu_update_info.metadata
        metadata_jwt_url = urllib.parse.urljoin(self.__url, metadata)
        self.__header_dict = _header_str_to_dict(ecu_update_info.header)
        logger.debug(f"[metadata_jwt] {metadata_jwt_url}")
        logger.debug(f"[header] {self.__header_dict}")

        #
        # download metadata
        #
        if not self._download_metadata(cfg.OTA_METADATA_FILE, metadata_jwt_url):
            # inform error
            self._inform_update_error("Can not get metadata!")
            # set 'NORMAL' state
            self._ota_status.set_ota_status(OtaStatusString.NORMAL_STATE)
            return False

        #
        # -----------------------------------------------------------
        # set 'METADATA' state
        # self._ota_status.set_ota_status('METADATA')

        next_bank = Path(self._grub_ctl.get_next_bank())
        if not self._construct_next_bank(next_bank, self._mount_point):
            # inform error
            self._inform_update_error("Can not construct update bank!")
            # set 'NORMAL' state
            self._ota_status.set_ota_status(OtaStatusString.NORMAL_STATE)
            _unmount_bank(self._mount_point)
            return False
        #
        # -----------------------------------------------------------
        # set 'PREPARED' state
        self._ota_status.set_ota_status(OtaStatusString.PREPARED_STATE)
        # unmount bank
        _unmount_bank(self._mount_point)
        return True

    def reboot(self):
        """
        Reboot
        """
        if self.get_ota_status() == OtaStatusString.PREPARED_STATE:
            # switch reboot
            if not self._grub_ctl.prepare_grub_switching_reboot(
                self._boot_vmlinuz, self._boot_initrd
            ):
                # inform error
                self._inform_update_error("Switching bank failed!")
                # set 'NORMAL' state
                self._ota_status.set_ota_status(OtaStatusString.NORMAL_STATE)
                _unmount_bank(self._mount_point)
                return False
            #
            # -----------------------------------------------------------
            # set 'SWITCHA/SWITCHB' state
            next_bank = self._grub_ctl.get_next_bank()
            next_state = self._get_switch_status_for_reboot(next_bank)
            self._ota_status.set_ota_status(next_state)
        #
        # reboot
        #
        self._grub_ctl.reboot()
        return True

    def save_update_ecuinfo(self):
        return _save_update_ecuinfo(
            self.__update_ecuinfo_yaml_file, self.__update_ecu_info
        )

    def _rollback(self):
        """
        Rollback
        """

        if self._ota_status.is_rollback_available() and self._rollback_dir.is_dir():
            #
            # OTA status
            #
            self._ota_status.dec_rollback_count()
            self._ota_status.set_ota_status(OtaStatusString.ROLLBACK_STATE)
            #
            # rollback /boot symlinks
            #

            # TODO: what is self._symlinklist_dir???
            # symlink_list_file = self._rollback_dir.joinpath(self._symlinklist_dir)
            # with open(symlink_list_file, "r") as f:
            #     for l in f.readlines():
            #         symlinkinf = SymbolicLinkInf(l)
            #         if os.path.dirname(symlinkinf.slink) == "/boot":
            #             rollback_link = os.path.join(
            #                 self._rollback_dir, os.path.basename(symlinkinf.slink)
            #             )
            #             if os.path.exists(rollback_link):
            #                 # if os.path.islink(symlinkinf.slink):
            #                 #    os.remove(symlinkinf.slink)
            #                 shutil.move(rollback_link, symlinkinf.slink)

            #
            # rollback /boot regulars
            #
            regular_list_file = self._rollback_dir.joinpath(
                self.backup_files["regularlist"]
            )

            with open(regular_list_file, "r") as f:
                for l in f.readlines():
                    reginf = RegularInf(l)
                    # if reginf.path.find('/boot/') == 0:
                    if Path("/boot") in reginf.path.parents:
                        rollback_file = self._rollback_dir.joinpath(reginf.path.name)

                        if rollback_file.exists():
                            # rollback file exist, restore file
                            shutil.move(rollback_file, reginf.path)
                        else:
                            # remove file
                            os.remove(reginf.path)
            #
            # rollback grub file
            #
            grub_file = self._rollback_dir.joinpath(self._grub_conf_file)
            if grub_file.exists():
                dest_file = self._grub_dir.joinpath(self._grub_conf_file)
                dest_file.unlink(missing_ok=True)
                shutil.move(grub_file, dest_file)
            #
            # rollback dir backup
            #
            rollback_backup = self._rollback_dir.with_suffix(
                self._rollback_dir.suffix + ".back"
            )
            if rollback_backup.exists():
                shutil.rmtree(rollback_backup)
            os.move(self._rollback_dir, rollback_backup)
            #
            # OTA status
            #
            # set 'SWITCHA/SWITCHB' state
            next_state = self._get_switch_status_for_reboot(
                self._grub_ctl.get_next_bank()
            )
            self._ota_status.set_ota_status(next_state)
            #
            # reboot
            #
            self._grub_ctl.reboot()
        else:
            logger.error("No available rollback.")
            return False
        return True
