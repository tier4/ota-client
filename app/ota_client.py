#!/usr/bin/env python3

import sys
import tempfile
import os
import pathlib
import stat
import shlex
import shutil
import subprocess
import urllib
import requests
import json
import yaml
import logging

import re
from hashlib import sha256

from ota_status import OtaStatus
from grub_control import GrubCtl
from ota_metadata import OtaMetaData

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class OtaError(Exception):
    """
    OTA error
    """
    pass


class Error(OSError):
    pass


def _decapsulate(name):
    return name[1:-1].replace("'\\''", "'")


def _file_sha256(filename):
    with open(filename, "rb") as f:
        digest = sha256(f.read()).hexdigest()
        return digest


def _get_separated_strings(string, search, num):
    arr = []
    curr = 0
    for i in range(num):
        pos = string[curr:].find(search)
        arr.append(string[curr : curr + pos])
        curr += pos + 1
    return arr, curr


def _find_file_separate(string):
    match = re.search(r"','(?!\\'')", string)  # find ',' not followed by \\''
    return match.start()


def _copy_complete(src_file, dst_file, follow_symlinks=False):

    src_dir = os.path.dirname(src_file)
    dst_dir = os.path.dirname(dst_file)
    _copydirs_complete(src_dir, dst_dir)
    shutil.copy2(src_file, dst_file, follow_symlinks=follow_symlinks)
    # copy owner and group
    st = os.stat(src_file)
    os.chown(dst_file, st[stat.ST_UID], st[stat.ST_GID])


def _copydirs_complete(src, dst):
    """
    copy directory path complete
    """
    if os.path.isdir(dst):
        # directory exist
        return True
    # check parent directory
    src_parent_dir = os.path.dirname(src)
    dst_parent_dir = os.path.dirname(dst)
    if os.path.isdir(dst_parent_dir) or _copydirs_complete(
        src_parent_dir, dst_parent_dir
    ):
        # parent exist, make directory
        logger.debug("mkdir: {dst}")
        os.mkdir(dst)
        shutil.copystat(src, dst, follow_symlinks=False)
        st = os.stat(src, follow_symlinks=False)
        os.chown(dst, st[stat.ST_UID], st[stat.ST_GID])
        return True
    return False


def _copytree_complete(src, dst):
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
        srcname = os.path.join(src, srcentry.name)
        dstname = os.path.join(dst, srcentry.name)
        try:
            if srcentry.is_symlink():
                linkto = os.readlink(srcname)
                os.symlink(linkto, dstname)
                st = os.stat(srcname, follow_symlinks=False)
                os.chown(
                    dstname, st[stat.ST_UID], st[stat.ST_GID], follow_symlinks=False
                )
            elif srcentry.is_dir():
                _copytree_complete(srcname, dstname)
            else:
                _copy_complete(srcname, dstname, follow_symlinks=False)
        except Error as e:
            errors.extend(e.args[0])
        except OSError as why:
            errors.append((srcname, dstname, str(why)))
    if errors:
        raise Error(errors)
    return dst


def _read_ecuid(ecuid_file):
    """
    initial read ECU ID
    """
    ecuid = ""
    logger.debug(f"ECU ID file: {ecuid_file}")
    try:
        if os.path.exists(ecuid_file):
            with open(ecuid_file, mode="r") as f:
                ecuid = f.readline().replace("\n", "")
                logger.debug(f"line: {ecuid}")
        else:
            logger.info(f"No ECU ID file!: {ecuid_file}")
    except:
        logger.warning("ECU ID read error!")
    return ecuid


def _read_ecu_info(ecu_info_yaml_file):
    """"""
    ecuinfo = {}
    if os.path.isfile(ecu_info_yaml_file):
        with open(ecu_info_yaml_file, "r") as fyml:
            logger.debug(f"open: {ecu_info_yaml_file}")
            ecuinfo = yaml.load(fyml, Loader=yaml.SafeLoader)
    else:
        logger.warning(f"No ECU info file: {ecu_info_yaml_file}")
    return ecuinfo


def _mount_bank(bank, target_dir):
    """
    mount next bank
    """
    try:
        command_line = "mount " + bank + " " + target_dir
        logger.debug(f"commandline: {command_line}")
        subprocess.check_output(shlex.split(command_line))
    except Exception as e:
        logger.exception("Mount error!:")
        return False
    return True

def _unmount_bank(target_dir):
    """
    unmount bank
    """
    try:
        if pathlib.Path(target_dir).is_mount():
            command_line = "umount " + target_dir
            logger.debug(f"commandline: {command_line}")
            subprocess.check_output(shlex.split(command_line))
    except Exception as e:
        logger.exception("Unmount error!:")
        return False
    return True


def _cleanup_dir(target_dir):
    """
    cleanup next bank
    """
    logger.debug(f"cleanup directory: {target_dir}")
    if target_dir == "" or target_dir == "/":
        return False
    try:
        command_line = "rm -rf " + str(target_dir) + "/*"
        logger.debug(f"commandline: {command_line}")
        # subprocess.check_output(shlex.split(command_line))
        # proc = subprocess.call(command_line.strip().split(" "))
        proc = subprocess.call(command_line, shell=True)
    except Exception as e:
        logger.exception("rm error!:")
        return False
    return True


def _gen_directories(dirlist_file, target_dir):
    """
    generate directories on another bank
    """
    try:
        with open(dirlist_file) as f:
            for l in f.read().splitlines():
                # logger.info(str(l))
                dirinf = DirectoryInf(l)
                # logger.info(f"dir inf: {dirinf.path}")
                # logger.info(f"target dir: {target_dir}")
                # target_path = os.path.join(target_dir, dirinf.path)
                target_path = target_dir + dirinf.path
                logger.debug(f"target path: {target_path}")
                os.makedirs(target_path, mode=int(dirinf.mode, 8))
                os.chown(target_path, int(dirinf.uid), int(dirinf.gpid))
                os.chmod(target_path, int(dirinf.mode, 8))
    except Exception as e:
        logger.exception("directory setup error!:")
        return False
    return True


def _exit_chroot(real_root, cwd):
    """
    exit chroot and back to cwd
    """
    # exit chroot
    os.fchdir(real_root)
    os.chroot(".")
    # Back to old root
    os.close(real_root)
    os.chdir(cwd)


def _gen_persistent_files(list_file, target_dir):
    """
    generate persistent files
    """
    res = True
    logging_level = logging.root.level
    logging.basicConfig(level=logging.DEBUG)
    with open(list_file, mode="r") as f:
        try:
            for l in f.read().splitlines():
                persistent_info = PersistentInf(l)
                src_path = persistent_info.path
                if src_path.find("/boot") == 0:
                    # /boot directory
                    logger.info(f"do nothing for boot dir file: {src_path}")
                else:
                    # others
                    if src_path[0] == "/":
                        dest_path = os.path.join(target_dir, "." + src_path)
                    else:
                        dest_path = os.path.join(target_dir, src_path)
                    if os.path.exists(src_path):
                        if os.path.isdir(src_path):
                            if os.path.exists(dest_path):
                                logger.debug(f"rmtree: {dest_path}")
                                shutil.rmtree(dest_path)
                            logger.debug(
                                f"persistent dir copy: {src_path} -> {dest_path}"
                            )
                            _copytree_complete(src_path, dest_path)
                        else:
                            logger.debug(
                                f"persistent file copy: {src_path} -> {dest_path}"
                            )
                            if os.path.exists(dest_path):
                                logger.info(f"rm file: {dest_path}")
                                os.remove(dest_path)
                            _copy_complete(src_path, dest_path)
                    else:
                        logger.warning(f"persistent file not exist: {src_path}")
        except Exception as e:
            logger.exception("persistent file error:")
            res = False
    logging.basicConfig(level=logging_level)
    return res


def _header_str_to_dict(header_str):
    """"""
    header_dict = {}
    for l in header_str.split(","):
        kl = l.split(":")
        if len(kl) == 2:
            header_dict[kl[0]] = kl[1]
    return header_dict


def _find_ecuinfo(ecuupdateinfo_list, ecu_id):
    """"""
    ecu_info = None
    for ecuupdateinfo in ecuupdateinfo_list:
        if ecu_id == ecuupdateinfo.ecu_info.ecu_id:
            logger.debug(f"[found] id={ecuupdateinfo.ecu_info.ecu_id}")
            ecu_info = ecuupdateinfo
    return ecu_info

def _save_update_ecuinfo(update_ecuinfo_yaml_file, update_ecu_info):
    """
    save update ecuinfo.yaml
    """
    output_file = update_ecuinfo_yaml_file
    logger.info(f"output_file: {output_file}")
    with tempfile.NamedTemporaryFile("w", delete=False) as ftmp:
        tmp_file_name = ftmp.name
        with open(tmp_file_name, "w") as f:
            f.write(yaml.dump(update_ecu_info))
            f.flush()
    shutil.move(tmp_file_name, output_file)
    os.sync()
    return True


class DirectoryInf:
    """
    Directory file information class
    """

    def __init__(self, info):
        line = info.replace("\n", "")
        info_list, last = _get_separated_strings(line, ",", 3)
        self.mode = info_list[0]
        self.uid = info_list[1]
        self.gpid = info_list[2]
        self.path = _decapsulate(line[last:])


class SymbolicLinkInf:
    """
    Symbolik link information class
    """

    def __init__(self, info):
        line = info.replace("\n", "")
        info_list, last = _get_separated_strings(line, ",", 3)
        self.mode = info_list[0]
        self.uid = info_list[1]
        self.gpid = info_list[2]
        sep_pos = _find_file_separate(line)
        self.slink = _decapsulate(line[last : sep_pos + 1])
        self.srcpath = _decapsulate(line[sep_pos + 2 :])


class RegularInf:
    """
    Regular file information class
    """

    def __init__(self, info):
        line = info.replace("\n", "")
        info_list, last = _get_separated_strings(line, ",", 5)
        self.mode = info_list[0]
        self.uid = info_list[1]
        self.gpid = info_list[2]
        self.links = info_list[3]
        self.sha256hash = info_list[4]
        self.path = _decapsulate(line[last:])


class PersistentInf:
    """
    Persistent file information class
    """

    def __init__(self, info):
        info_list = info.replace("\n", "").split(",")
        self.path = _decapsulate(info_list[0])


class OtaCache:
    def __init__(self, directory="/tmp/ota-cache"):
        self._directory = directory
        os.makedirs(self._directory, exist_ok=True)

    def save(self, name):
        dst = os.path.normpath(self._directory + name)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _copy_complete(name, dst)

    def restore(self, name, target_hash):
        src = os.path.normpath(self._directory + name)
        if os.path.isfile(src) and _file_sha256(src) == target_hash:
            _copy_complete(src, name)
            return True
        return False


class OtaClient:
    """
    OTA Client class
    """

    def __init__(
        self,
        boot_status="NORMAL_BOOT",
        url="",
        ota_status_file="/boot/ota/ota_status",
        bank_info_file="/boot/ota/bankinfo.yaml",
        ecuid_file="/boot/ota/ecuid",
        ecuinfo_yaml_file="/boot/ota/ecuinfo.yaml",
        ota_cache=None,
    ):
        """
        OTA Client initialize
        """
        self.__main_ecu = True
        self.__download_retry = 5
        self._boot_status = boot_status
        #
        self.__url = url
        self.__header_dict = {}
        #
        self.__my_ecuid = _read_ecuid(ecuid_file)
        self.__ecuinfo_yaml_file = ecuinfo_yaml_file
        self.__ecu_info = _read_ecu_info(ecuinfo_yaml_file)
        self.__update_ecuinfo_yaml_file = self.__ecuinfo_yaml_file + ".update"
        self.__update_ecu_info = self.__ecu_info

        self._ota_dir = "/boot/ota"
        self._rollback_dir = "/boot/ota/rollback"
        self._grub_dir = "/boot/grub"
        self._grub_conf_file = "grub.conf"
        #
        self._catalog_file = "/boot/ota/.catalog"
        self._rollback_dict = {}
        # backup files
        self._dirlist_file = "dirlist.txt"
        self._symlinklist_file = "symlinklist.txt"
        self._regularlist_file = "regularlist.txt"
        self._persistentlist_file = "persistentlist.txt"
        #
        self._ota_status = OtaStatus(ota_status_file=ota_status_file)
        self._grub_ctl = GrubCtl(bank_info_file=bank_info_file)
        #
        self._certificate_pem = None
        # metadata data
        self._metadata = None
        #
        self._mount_point = "/mnt/bank"
        self._fstab_file = "/etc/fstab"
        if not os.path.isdir(self._mount_point):
            os.makedirs(self._mount_point, exist_ok=True)
        self._ota_cache = ota_cache
        self._boot_vmlinuz = None
        self._boot_initrd = None

    def is_main_ecu(self):
        return self.__main_ecu

    def get_my_ecuid(self):
        return self.__my_ecuid

    def get_ecuinfo(self):
        """"""
        return self.__ecu_info

    def get_boot_status(self):
        return self._boot_status

    def get_ota_status(self):
        return self._ota_status.get_ota_status()

    def _set_url(self, url):
        self.__url = url

    def _get_metadata_jwt_url(self):
        """
        get metadata.jwt URL for test
        """
        return os.path.join(self.__url, "metadata.jwt")

    def _is_banka(self, bank):
        return self._grub_ctl.get_bank_info().get_banka() == bank

    def _is_bankb(self, bank):
        return self._grub_ctl.get_bank_info().get_bankb() == bank

    def _get_current_bank(self):
        return self._grub_ctl.get_bank_info().get_current_bank()

    def _get_current_bank_uuid(self):
        return self._grub_ctl.get_bank_info().get_current_bank_uuid()

    def _get_next_bank(self):
        return self._grub_ctl.get_bank_info().get_next_bank()

    def _get_next_bank_uuid(self):
        return self._grub_ctl.get_bank_info().get_next_bank_uuid()

    def _get_metadata_url(self):
        """
        get metadata URL
        """
        return os.path.join(self.__url, "metadata.jwt")

    def _download_raw(self, url, target_file):
        """"""
        header = self.__header_dict  # self.__cookie
        header["Accept-encording"] = "gzip"
        response = requests.get(url, headers=header)
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
                # NOTE: CRITICAL:50, ERROR:40, WARNING:30, INFO:20, DEBUG:10
                if logging.root.level <= logging.DEBUG:
                    done = int(50 * dl / total_length)
                    sys.stdout.write("\r[%s%s]" % ("=" * done, " " * (50 - done)))
                    sys.stdout.flush()
        target_file.flush()
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

    def _download_raw_file(self, url, dest_file, target_hash="", fl=""):
        """
        download file
        """
        logger.debug(f"DL File: {dest_file}")
        digest = ""
        try:
            with tempfile.NamedTemporaryFile("wb", delete=False) as ftmp:
                tmp_file_name = ftmp.name
                logger.debug(f"temp_file: {tmp_file_name}")
                # download
                response, digest = self._download_raw(url, ftmp)
                if response.status_code != 200:
                    logger.error(f"status_code={response.status_code}, url={url}")
                    return False
            # file move
            shutil.move(tmp_file_name, dest_file)
        except Exception as e:
            logger.exception("File download error!:")
            return False
        finally:
            if os.path.isfile(tmp_file_name):
                os.remove(tmp_file_name)
        # check sha256 hash
        if target_hash != "" and digest != target_hash:
            logger.error(f"hash missmatch: {dest_file}")
            logger.error(f"  dl hash: {digest}")
            logger.error(f"  hash: {target_hash}")
            if fl != "":
                fl.write("hash missmatch: " + dest_file + "\n")
            return False
        return True

    def _download_raw_file_with_retry(self, url, dest_file, target_hash="", fl=""):
        """"""
        for i in range(self.__download_retry):
            if self._download_raw_file(url, dest_file, target_hash, fl):
                logger.debug(f"retry count: {i}")
                return True
        return False

    def _download_metadata_jwt(self, metadata_url):
        """
        Download metadata.jwt
        """
        # url = self._get_metadata_url()
        return self._download(metadata_url)

    def _download_metadata_jwt_file(self, dest_file):
        """
        Download metadata.jwt file(for debagging)
        """
        try:
            # download metadata.jwt
            metadata_url = self._get_metadata_url()
            dest_path = "/tmp/" + dest_file
            logger.debug(f"url: {metadata_url}")
            logger.debug(f"metadata dest path: {dest_path}")
            if not self._download_raw_file_with_retry(metadata_url, dest_path):
                return False
        except Exception as e:
            logger.exception("metadata error!:")
            return False
        return True

    def _download_metadata(self, metadata_url):
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
                with open("/boot/ota/metadata.jwt", "w") as f:
                    f.write(self._metadata_jwt)
                self._metadata = OtaMetaData(
                    self._metadata_jwt
                )
            else:
                self._metadata_jwt = ""
        except Exception as e:
            self._meta_data_file = ""
            logger.exception("Error: OTA meta data download fail.:")
            return False
        return True

    def _get_certificate_url(self):
        """
        Get certificate file(temp.)
        """
        return os.path.join(self.__url, "ota-intermediate.pem")

    def _download_certificate(self):
        """
        Download certificate file
        """
        url = self._get_certificate_url()
        return self._download(url)

    def _verify_metadata_jwt(self, metadata):
        """
        verify metadata.jwt
        """
        cert_file, cert_hash = metadata.get_certificate_info()
        response = self._download_certificate()
        if response.status_code == 200:
            pem = response.text
            if sha256(pem.encode()).hexdigest() != cert_hash:
                logger.error("certificate hash missmatch:")
                logger.error(f"    dl hash: {sha256(pem).hexdigest()}")
                logger.error(f"    hash: {cert_hash}")
                return False
            return metadata.verify(pem)
        else:
            logger.error(f"response error: {response.status_code}")
            return False
        return True

    def _cleanup_rollback_dir(self):
        """"""
        try:
            if os.path.isdir(self._rollback_dir):
                logger.info(f"removedir: {self._rollback_dir}")
                shutil.rmtree(self._rollback_dir)
            logger.debug(f"makedir: {self._rollback_dir}")
            os.mkdir(self._rollback_dir)
        except Exception as e:
            logger.exception("cleanup rollback directory error!:")
            return False
        return True

    def _prepare_next_bank(self, bank, target_dir):
        """
        prepare next boot bank
            mount & clean up
        """
        # mount
        if not _mount_bank(bank, target_dir):
            return False
        # cleanup
        if not _cleanup_dir(target_dir):
            _unmount_bank(target_dir)
            return False
        # clean rollback dir
        if not self._cleanup_rollback_dir():
            _unmount_bank(target_dir)
            return False
        return True

    def _download_list_file(self, url, list_file, hash=""):
        """
        Download list file(debug)
        """
        dirs_url = os.path.join(url, list_file)
        dest_path = os.path.join("/tmp", list_file)
        return self._download_raw_file_with_retry(dirs_url, dest_path, hash)


    def _setup_directories(self, target_dir):
        """
        generate directories on another bank
        """
        # get directories metadata
        dirs_file, dirs_hash = self._metadata.get_directories_info()
        dirs_url = os.path.join(self.__url, dirs_file)
        tmp_list_file = os.path.join("/tmp", dirs_file)
        if self._download_raw_file_with_retry(dirs_url, tmp_list_file, dirs_hash):
            # generate directories
            if _gen_directories(tmp_list_file, target_dir):
                # move list file to rollback dir
                dest_file = os.path.join(self._rollback_dir, self._dirlist_file)
                shutil.move(tmp_list_file, dest_file)
                return True
        return False


    def _gen_symbolic_links(self, symlinks_file, target_dir):
        """
        generate symbolic_links on another bank
        """
        res = True
        with open(symlinks_file, mode="r") as f:
            try:
                cwd = os.getcwd()
                real_root = os.open("/", os.O_RDONLY)
                os.chroot(target_dir)
                # Chrooted environment
                # os.chdir(target_dir)
                for l in f.read().splitlines():
                    slinkf = SymbolicLinkInf(l)
                    logger.debug(f"src: {slinkf.srcpath}")
                    logger.debug(f"slink: {slinkf.slink}")
                    if slinkf.slink.find("/boot") == 0:
                        # /boot directory
                        # exit chroot environment
                        _exit_chroot(real_root, cwd)
                        try:
                            dest_file = ""
                            if os.path.exists(slinkf.slink):
                                dest_dir = self._rollback_dir + "/"
                                shutil.move(slinkf.slink, dest_dir)
                            os.symlink(slinkf.srcpath, slinkf.slink)
                        except Exception as e:
                            logger.exception("symbolic link error!")
                            if dest_file != "":
                                shutil.move(dest_file, slinkf.slink)
                            raise (OtaError("Cannot make symbolic link."))
                        # re-enter the chrooted environment
                        os.chroot(target_dir)
                    else:
                        # others
                        os.symlink(slinkf.srcpath, slinkf.slink)
            except Exception as e:
                logger.exception("symboliclink error:")
                res = False
            finally:
                # exit chroot
                _exit_chroot(real_root, cwd)
        return res

    def _setup_symboliclinks(self, target_dir):
        """
        generate symboliclinks on another bank
        """
        # get symboliclink metadata
        symlinks_file, syminks_hash = self._metadata.get_symboliclinks_info()
        symlinks_url = os.path.join(self.__url, symlinks_file)
        tmp_list_file = os.path.join("/tmp", symlinks_file)
        if self._download_raw_file_with_retry(
            symlinks_url, tmp_list_file, syminks_hash
        ):
            # generate symboliclinks
            if self._gen_symbolic_links(tmp_list_file, target_dir):
                # move list file to rollback dir
                dest_file = os.path.join(self._rollback_dir, self._symlinklist_file)
                shutil.move(tmp_list_file, dest_file)
                return True
        return False

    # @staticmethod
    # def _make_url_path(url, rootfs_dir, regular_file):
    #    return os.path.join(url, rootfs_dir + urllib.parse.quote(regular_file))

    def _download_regular_file(
        self, rootfs_dir, target_path, regular_file, hash256, fl
    ):
        """
        Download regular file
        """
        # download new file
        regular_url = os.path.join(
            self.__url, rootfs_dir + urllib.parse.quote(regular_file)
        )
        logger.debug(f"download file: {regular_url}")
        return self._download_raw_file_with_retry(regular_url, target_path, hash256, fl)

    def _gen_boot_dir_file(self, rootfs_dir, target_dir, regular_inf, prev_inf, fl):
        """
        generate /boot directory file
        """
        # starts with `/boot/vmlinuz-`.
        match = re.match("^/boot/(vmlinuz-.*)", regular_inf.path)
        if match is not None:
            self._boot_vmlinuz = match.group(1)

        # starts with `/boot/initrd.img-`, but doesnot end with `.old-dkms`.
        match = re.match("^(?!.*\.old-dkms$)/boot/(initrd\.img-.*)", regular_inf.path)
        if match is not None:
            self._boot_initrd = match.group(1)

        if (
            int(regular_inf.links) >= 2
            and prev_inf != ""
            and prev_inf.sha256hash == regular_inf.sha256hash
        ):
            # create hard link
            logger.debug(f"links: {regular_inf.links}")
            os.link(prev_inf.path, regular_inf.path)
        else:
            # no hard link
            if (
                os.path.isfile(regular_inf.path)
                and _file_sha256(regular_inf.path) == regular_inf.sha256hash
            ):
                # nothing to do
                rollback_file = os.path.join(
                    self._rollback_dir, os.path.basename(regular_inf.path)
                )
                _copy_complete(regular_inf.path, rollback_file)
                self._rollback_dict[regular_inf.path] = regular_inf.path
                logger.debug("file already exist! no copy or download!")
            else:
                if os.path.isfile(regular_inf.path):
                    # backup for rollback
                    rollback_file = os.path.join(
                        self._rollback_dir, os.path.basename(regular_inf.path)
                    )
                    _copy_complete(regular_inf.path, rollback_file)
                    self._rollback_dict[regular_inf.path] = rollback_file
                else:
                    self._rollback_dict[regular_inf.path] = ""
                # download new file
                if self._download_regular_file(
                    rootfs_dir,
                    regular_inf.path,
                    regular_inf.path,
                    regular_inf.sha256hash,
                    fl,
                ):
                    logger.debug(f"Download: {regular_inf.path}")
                    logger.debug(f"file hash: {regular_inf.sha256hash}")
                else:
                    raise OtaError("File down load error!")
                logger.debug(f"regular_file: {regular_inf.path}")
                os.chown(regular_inf.path, int(regular_inf.uid), int(regular_inf.gpid))
                os.chmod(regular_inf.path, int(regular_inf.mode, 8))

    def _gen_regular_file(self, rootfs_dir, target_dir, regular_inf, prev_inf, fl):
        """
        generate regular file
        """
        dest_path = os.path.join(target_dir, "." + regular_inf.path)
        if (
            int(regular_inf.links) >= 2
            and prev_inf.sha256hash == regular_inf.sha256hash
        ):
            # create hard link
            logger.debug(f"links: {regular_inf.links}")
            src_path = os.path.join(target_dir, "." + prev_inf.path)
            os.link(src_path, dest_path)
        else:
            # no hard link
            logger.debug(f"No hard links: {regular_inf.links}")
            current_file = os.path.join("/", "." + regular_inf.path)
            if (
                os.path.isfile(current_file)
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
                    pass
                # download new file
                elif self._download_regular_file(
                    rootfs_dir, dest_path, regular_inf.path, regular_inf.sha256hash, fl
                ):
                    if self._ota_cache is not None:
                        self._ota_cache.save(dest_path)
                    logger.debug(f"Download: {regular_inf.path}")
                    logger.debug(f"file hash: {regular_inf.sha256hash}")
                else:
                    raise OtaError("File down load error!")
            logger.debug(f"regular_file: {dest_path}")
            logger.debug(f"permissoin: {str(regular_inf.mode)}")
            os.chown(dest_path, int(regular_inf.uid), int(regular_inf.gpid))
            os.chmod(dest_path, int(regular_inf.mode, 8))


    def _gen_regular_files(self, rootfs_dir, regulars_file, target_dir):
        """
        generate regular files
        """
        self._boot_vmlinuz = None  # clear
        self._boot_initrd = None  # clear
        with tempfile.NamedTemporaryFile(delete=False) as flog:
            log_file = flog.name
            with open(flog.name, "w") as fl:
                res = True
                with open(regulars_file) as f:
                    try:
                        cwd = os.getcwd()
                        os.chdir(target_dir)
                        logger.debug(f"target_dir: {target_dir}")
                        prev_inf = ""
                        rlist = []
                        for l in f.readlines():
                            rlist.append(RegularInf(l))
                        sorted_rlist = sorted(rlist, key=lambda x: x.sha256hash)
                        # with open('./tests/sorted_regular_list.txt', "w") as fdst:
                        #    for lst in sorted_list:
                        #        fdst.write(str(lst) + '\n')
                        for l in sorted_rlist:
                            regular_inf = l
                            logger.debug(
                                f"file: {regular_inf.path}, hash: {regular_inf.sha256hash}"
                            )
                            if regular_inf.path.find("/boot/") == 0:
                                # /boot directory file
                                logger.debug(f"boot file: {regular_inf.path}")
                                self._gen_boot_dir_file(
                                    rootfs_dir, target_dir, regular_inf, prev_inf, fl
                                )
                            else:
                                # others
                                logger.debug(f"no boot file: {regular_inf.path}")
                                self._gen_regular_file(
                                    rootfs_dir, target_dir, regular_inf, prev_inf, fl
                                )
                            prev_inf = regular_inf
                    except Exception as e:
                        logger.exception("regular files setup error!:")
                        res = False
                    finally:
                        os.chdir(cwd)
        shutil.copy(log_file, "./regulars_dl.log")
        return res

    def _setup_regular_files(self, target_dir):
        """
        update files copy to another bank
        """
        rootfs_dir = self._metadata.get_rootfsdir_info()
        # get regular metadata
        regularslist_file, regularslist_hash = self._metadata.get_regulars_info()
        regularslist_url = os.path.join(self.__url, regularslist_file)
        tmp_list_file = os.path.join("/tmp", regularslist_file)
        if self._download_raw_file_with_retry(
            regularslist_url, tmp_list_file, regularslist_hash
        ):
            if self._gen_regular_files(rootfs_dir, tmp_list_file, target_dir):
                # move list file to rollback dir
                dest_file = os.path.join(self._rollback_dir, self._regularlist_file)
                shutil.move(tmp_list_file, dest_file)
                return True
            if self._boot_vmlinuz is None or self._boot_initrd is None:
                logging.warning(
                    "vmlinuz or initrd is not set. This condition will be treated as an error in the future."
                )
        return False

    def _setup_persistent_files(self, target_dir):
        """
        setup persistent files
        """
        if self._metadata.is_persistent_enabled():
            # get persistent metadata
            persistent_file, persistent_hash = self._metadata.get_persistent_info()
            persistent_url = os.path.join(self.__url, persistent_file)
            tmp_list_file = os.path.join("/tmp", persistent_file)
            if not self._download_raw_file_with_retry(
                persistent_url, tmp_list_file, persistent_hash
            ):
                logger.error(f"persistent file download error: {persistent_url}")
                return False
            else:
                logger.info(f"persistent file download success: {persistent_url}")
        else:
            # read list from the local file
            tmp_list_file = self._persistentlist_file

        if _gen_persistent_files(tmp_list_file, target_dir):
            # move list file to rollback dir
            dest_file = os.path.join(self._rollback_dir, self._persistentlist_file)
            shutil.move(tmp_list_file, dest_file)
            return True
        return False

    def _setup_next_bank_fstab(self, fstab_file, target_dir):
        """
        setup next bank to fstab
        """
        if not os.path.isfile(fstab_file):
            logger.error(f"file not exist: {fstab_file}")
            return False

        dest_fstab_file = os.path.join(target_dir, "." + fstab_file)

        with tempfile.NamedTemporaryFile(delete=False) as ftmp:
            tmp_file = ftmp.name
            with open(ftmp.name, "w") as fout:
                with open(fstab_file, "r") as f:
                    lines = f.readlines()
                    for l in lines:
                        if l[0] == "#":
                            fout.write(l)
                            continue
                        fstab_list = l.split()
                        if fstab_list[1] == "/":
                            lnext = ""
                            current_bank = self._get_current_bank()
                            next_bank = self._get_next_bank()
                            current_bank_uuid = self._get_current_bank_uuid()
                            next_bank_uuid = self._get_next_bank_uuid()
                            if fstab_list[0].find(current_bank) >= 0:
                                # devf found
                                lnext = l.replace(current_bank, next_bank)
                            elif fstab_list[0].find(current_bank_uuid) >= 0:
                                # uuid found
                                lnext = l.replace(current_bank_uuid, next_bank_uuid)
                            elif (
                                fstab_list[0].find(current_bank) >= 0
                                or fstab_list[0].find(next_bank_uuid) >= 0
                            ):
                                # next bank found
                                logger.debug("Already set to next bank!")
                                lnext = l
                            else:
                                raise (Exception("root device mismatch in fstab."))
                            fout.write(lnext)
                        else:
                            fout.write(l)
                fout.flush()
        # replace to new fstab file
        if os.path.exists(dest_fstab_file):
            _copy_complete(dest_fstab_file, dest_fstab_file + ".old")
        shutil.move(tmp_file, dest_fstab_file)

        return True

    def _construct_next_bank(self, next_bank, target_dir):
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

    def _get_switch_status_for_reboot(self, next_bank):
        """
        get switch status for reboot
        """
        if self._grub_ctl.get_bank_info().is_banka(next_bank):
            return "SWITCHA"
        elif self._grub_ctl.get_bank_info().is_bankb(next_bank):
            return "SWITCHB"
        raise Exception("Bank is not A/B bank!")

    def _inform_update_error(self, error):
        """
        inform update error
        """
        logger.error(f"Update error!: {str(error)}")
        # ToDO : implement

        return

    def _load_cookie(self):
        """
        read cookie form file
        """
        with open(self._cookie_file) as f:
            self.cookie = f.readline()

    def _load_url(self):
        """
        read url from file
        """
        with open(self._url_file) as f:
            self.__url = f.readline()

    def _load_catalog(self):
        """
        read catalog from file
        """
        with open(self._catalog_file) as f:
            catalog_json = json.load(f)
            # toDo: perse catalog

    def _load_update_files(self):
        """"""
        try:
            self._load_url()
            self._load_cookie()
            self._load_catalog()
        except Exception as e:
            logger.exception("Load update files error:")
            return False
        return True


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
        return self._update(ecu_update_info, reboot=False)


    def _update(self, ecu_update_info, reboot=False):
        """
        OTA update execution
        """
        # -----------------------------------------------------------
        # set 'UPDATE' state
        self._ota_status.set_ota_status("UPDATE")
        logger.debug(ecu_update_info)
        self.__url = ecu_update_info.url
        metadata = ecu_update_info.metadata
        metadata_jwt_url = os.path.join(self.__url, metadata)
        self.__header_dict = _header_str_to_dict(ecu_update_info.header)
        logger.debug(f"[metadata_jwt] {metadata_jwt_url}")
        logger.debug(f"[header] {self.__header_dict}")

        #
        # download metadata
        #
        if not self._download_metadata(metadata_jwt_url):
            # inform error
            self._inform_update_error("Can not get metadata!")
            # set 'NORMAL' state
            self._ota_status.set_ota_status("NORMAL")
            return False

        #
        # -----------------------------------------------------------
        # set 'METADATA' state
        # self._ota_status.set_ota_status('METADATA')

        next_bank = self._get_next_bank()
        if not self._construct_next_bank(next_bank, self._mount_point):
            # inform error
            self._inform_update_error("Can not construct update bank!")
            # set 'NORMAL' state
            self._ota_status.set_ota_status("NORMAL")
            _unmount_bank(self._mount_point)
            return False
        #
        # -----------------------------------------------------------
        # set 'PREPARED' state
        self._ota_status.set_ota_status("PREPARED")
        if reboot:
            self.reboot()

        return True


    def reboot(self):
        if self.get_ota_status() == "PREPARED":
            # switch reboot
            if not self._grub_ctl.prepare_grub_switching_reboot(
                self._boot_vmlinuz, self._boot_initrd
            ):
                # inform error
                self._inform_update_error("Switching bank failed!")
                # set 'NORMAL' state
                self._ota_status.set_ota_status("NORMAL")
                _unmount_bank(self._mount_point)
                return False
            #
            # -----------------------------------------------------------
            # set 'SWITCHA/SWITCHB' state
            next_bank = self._get_next_bank()
            next_state = self._get_switch_status_for_reboot(next_bank)
            self._ota_status.set_ota_status(next_state)
        #
        # reboot
        #
        os.sync()
        self._grub_ctl.reboot()
        return True

    def find_ecuinfo(self, ecuupdateinfo_list, ecu_id):
        return _find_ecuinfo(ecuupdateinfo_list, ecu_id)

    def save_update_ecuinfo(self):
        return _save_update_ecuinfo(self.__update_ecuinfo_yaml_file, self.__update_ecu_info)

    def _rollback(self):
        """"""
        if self.get_ota_status() == "NORMAL":
            return False

        if self._ota_status.is_rollback_available() and os.path.isdir(
            self._rollback_dir
        ):
            #
            # OTA status
            #
            self._ota_status.dec_rollback_count()
            self._ota_status.set_ota_status("ROLLBACK")
            #
            # rollback /boot symlinks
            #
            symlink_list_file = os.path.join(self._rollback_dir, self._symlinklist_dir)
            with open(symlink_list_file, "r") as f:
                for l in f.readlines():
                    symlinkinf = SymbolicLinkInf(l)
                    if os.path.dirname(symlinkinf.slink) == "/boot":
                        rollback_link = os.path.join(
                            self._rollback_dir, os.path.basename(symlinkinf.slink)
                        )
                        if os.path.exists(rollback_link):
                            # if os.path.islink(symlinkinf.slink):
                            #    os.remove(symlinkinf.slink)
                            shutil.move(rollback_link, symlinkinf.slink)
            #
            # rollback /boot regulars
            #
            regular_list_file = os.path.join(self._rollback_dir, self._regularlist_file)
            with open(regular_list_file, "r") as f:
                for l in f.readlines():
                    reginf = RegularInf(l)
                    # if reginf.path.find('/boot/') == 0:
                    if os.path.dirname(reginf.path) == "/boot":
                        rollback_file = os.path.join(
                            self._rollback_dir, os.path.basename(reginf.path)
                        )
                        if os.path.exists(rollback_file):
                            # rollback file exist, restore file
                            shutil.move(rollback_file, reginf.path)
                        else:
                            # remove file
                            os.remove(reginf.path)
            #
            # rollback grub file
            #
            grub_file = os.path.join(self._rollback_dir, self._grub_conf_file)
            if os.path.exists(grub_file):
                dest_file = os.path.join(self._grub_dir, self._grub_conf_file)
                if os.path.exists(dest_file):
                    os.remove(dest_file)
                shutil.move(grub_file, dest_file)
            #
            # rollback dir backup
            #
            rollback_backup = self._rollback_dir + ".back"
            if os.path.exists(rollback_backup):
                shutil.rmtree(rollback_backup)
            os.move(self._rollback_dir, rollback_backup)
            #
            # OTA status
            #
            # set 'SWITCHA/SWITCHB' state
            next_state = self._get_switch_status_for_reboot(self._get_next_bank())
            self._ota_status.set_ota_status(next_state)
            #
            # reboot
            #
            os.sync()
            self._grub_ctl.reboot()
        else:
            logger.error("No available rollback.")
            return False
        return True
