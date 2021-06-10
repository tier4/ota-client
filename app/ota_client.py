#!/usr/bin/env python3

# import time
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
import base64
import yaml

# import gzip
import re
from operator import itemgetter
from hashlib import sha256, sha1

# from Crypto.Hash import SHA256
# from Crypto.Signature import PKCS1_v1_5
# from Crypto.PublicKey import RSA
from OpenSSL import crypto


# from bank import BankInfo
from ota_status import OtaStatus
from grub_control import GrubCtl
from ota_metadata import OtaMetaData

import otaclient_pb2


class OtaError(Exception):
    """
    OTA error
    """

    pass


def Error(OSError):
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


def _copy_complete(src_file, dest_file, follow_symlinks=False):
    shutil.copy2(src_file, dest_file, follow_symlinks=follow_symlinks)
    # copy owner and group
    st = os.stat(src_file)
    os.chown(dest_file, st[stat.ST_UID], st[stat.ST_GID])


def _copytree_complete(
    src, dst, symlinks=False, ignore_dangling_symlinks=False, dirs_exist_ok=False
):
    """
    directory complete copy
    """
    # get directory entories
    with os.scandir(src) as itr:
        entries = list(itr)
    # make directory on the destination
    os.makedirs(dst, exist_ok=dirs_exist_ok)
    errors = []
    # copy entries
    for srcentry in entries:
        srcname = os.path.join(src, srcentry.name)
        dstname = os.path.join(dst, srcentry.name)
        try:
            if srcentry.is_symlink():
                linkto = os.readlink(srcname)
                if symlinks:
                    os.symlink(linkto, dstname)
                    shutil.copystat(srcname, dstname, follow_symlinks=not symlinks)
                    st = os.stat(srcname)
                    os.chown(dstname, st[stat.ST_UID], st[stat.ST_GID])
                else:
                    if not os.path.exists(linkto) and ignore_dangling_symlinks:
                        continue
                    if srcentry.is_dir():
                        _copytree_complete(
                            srcname, dstname, symlinks, dirs_exist_ok=dirs_exist_ok
                        )
                    else:
                        _copy_complete(srcname, dstname, follow_symlinks=not symlinks)
            elif srcentry.is_dir():
                _copytree_complete(
                    srcname, dstname, symlinks, dirs_exist_ok=dirs_exist_ok
                )
            else:
                _copy_complete(srcname, dstname, follow_symlinks=not symlinks)
        except Error as e:
            errors.extend(e.args[0])
        except OSError as why:
            errors.append((srcname, dstname, str(why)))
    try:
        shutil.copystat(src, dst)
        st = os.stat(src)
        os.chown(dst, st[stat.ST_UID], st[stat.ST_GID])
    except OSError as why:
        errors.append((src, dst, str(why)))
    if errors:
        raise Error(errors)
    return dst


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
        policy_file="",
        pem_file="",
        ota_cache=None,
    ):
        """
        OTA Client initialize
        """
        self.__main_ecu = True
        self.__verbose = False
        self.__download_retry = 5
        self._boot_status = boot_status
        #
        self.__url = url
        self.__header_dict = {}
        #
        self.__my_ecuid = self._read_ecuid(ecuid_file)
        self.__ecuinfo_yaml_file = ecuinfo_yaml_file
        self.__ecu_info = self._read_ecu_info(ecuinfo_yaml_file)
        self.__update_ecuinfo_yaml_file = self.__ecuinfo_yaml_file + ".update"
        self.__update_ecu_info = self.__ecu_info

        self._ota_dir = "/boot/ota"
        self._rollback_dir = "/boot/ota/rollback"
        self._grub_dir = "/boot/grub"
        self._grub_conf_file = "grub.conf"
        #
        # self._url_file = '/boot/ota/.url'
        self._catalog_file = "/boot/ota/.catalog"
        # self._metadata_jwt_file = 'metadata.jwt'
        # self._cookie_file = '/boot/ota/.cookie'
        self._rollback_dict = {}
        # backup files
        self._dirlist_file = "dirlist.txt"
        self._symlinklist_file = "symlinklist.txt"
        self._regularlist_file = "regularlist.txt"
        self._persistentlist_file = "persistentlist.txt"
        #
        if not os.path.exists(ota_status_file):
            self._gen_ota_status_file(ota_status_file)
        self._ota_status = OtaStatus(ota_status_file=ota_status_file)
        self._grub_ctl = GrubCtl(bank_info_file=bank_info_file)
        #
        self._certificate_pem = ""
        # metadata data
        self._metadata = ""
        #
        self._mount_point = "/mnt/bank"
        self._fstab_file = "/etc/fstab"
        if not os.path.isdir(self._mount_point):
            os.makedirs(self._mount_point, exist_ok=True)
        self._ota_cache = ota_cache

    def _gen_ota_status_file(self, ota_status_file):
        """
        generate OTA status file
        """
        with tempfile.NamedTemporaryFile(delete=False) as ftmp:
            tmp_file = ftmp.name
            with open(ftmp.name, "w") as f:
                f.write("NORMAL")
                f.flush()
        os.sync()
        dir_name = os.path.dirname(ota_status_file)
        if not os.path.exists(dir_name):
            os.makedirs(dir_name)
        shutil.move(tmp_file, ota_status_file)
        print(ota_status_file, " generated.")
        os.sync()
        return True

    def is_main_ecu(self):
        return self.__main_ecu

    def _read_ecuid(self, ecuid_file):
        """
        initial read ECU ID
        """
        ecuid = ""
        if self.__verbose:
            print("ECU ID file: " + ecuid_file)
        try:
            if os.path.exists(ecuid_file):
                with open(ecuid_file, mode="r") as f:
                    ecuid = f.readline().replace("\n", "")
                    if self.__verbose:
                        print("line: ", ecuid)
            else:
                print("No ECU ID file!:", ecuid_file)
        except:
            print("ECU ID read error!")
        return ecuid

    def _read_ecu_info(self, ecu_info_yaml_file):
        """"""
        ecuinfo = {}
        if os.path.isfile(ecu_info_yaml_file):
            with open(ecu_info_yaml_file, "r") as fyml:
                if self.__verbose:
                    print("open: ", ecu_info_yaml_file)
                ecuinfo = yaml.load(fyml, Loader=yaml.SafeLoader)
        else:
            print("No ECU info file: ", ecu_info_yaml_file)
        return ecuinfo

    def get_my_ecuid(self):
        return self.__my_ecuid

    def _get_ecu_info(self):
        """"""
        return self.__ecu_info

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
            print("download error: ", response.status_code)
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
                if self.__verbose:
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
        if self.__verbose:
            print("encording: ", response.encoding)
        # print(response.text)
        return response

    def _download_raw_file(self, url, dest_file, target_hash="", fl=""):
        """
        download file
        """
        result = True
        digest = ''
        try:
            with tempfile.NamedTemporaryFile("wb", delete=False) as ftmp:
                tmp_file_name = ftmp.name
                if self.__verbose:
                    print("temp_file: ", tmp_file_name)
                # download
                response, digest = self._download_raw(url, ftmp)
                if response.status_code != 200:
                    print("download error! status code: ", response.status_code)
                    return False
                    if self.__verbose:
                        print("response status code: ", response.status_code)
                else:
                    print("download error! status code: ", response.status_code)
                    result = False
            # file move
            shutil.move(tmp_file_name, dest_file)
        except Exception as e:
            print("File download error!: ", e)
            result = False
        finally:
            if os.path.isfile(tmp_file_name):
                os.remove(tmp_file_name)
        if result:
            # check sha256 hash
            if self.__verbose:
                print("DL File: ", dest_file)
            if target_hash != "" and digest != target_hash:
                print("hash missmatch: ", dest_file)
                print("  dl hash: ", digest)
                print("  hash: ", target_hash)
                if fl != "":
                    fl.write("hash missmatch: " + dest_file + "\n")
                result = False
        return result

    def _download_raw_file_with_retry(self, url, dest_file, target_hash="", fl=""):
        """"""
        for i in range(self.__download_retry):
            if self._download_raw_file(url, dest_file, target_hash, fl):
                if self.__verbose:
                    print("retry count: ", i)
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
            if self.__verbose:
                print("url: ", metadata_url)
                print("metadata dest path: ", dest_path)
            if not self._download_raw_file_with_retry(metadata_url, dest_path):
                return False
        except Exception as e:
            print("metadata error!:", e)
            return False
        return True

    def _download_metadata(self, metadata_url):
        """
        Download OTA metadata
        """
        try:
            # dounload and write meta data.
            # url = self._get_metadata_url()
            if self.__verbose:
                print("metadata url: ", metadata_url)
            response = self._download(metadata_url)
            if self.__verbose:
                print("response: ", response.status_code)
            if response.status_code == 200:
                self._metadata_jwt = response.text
                with open("/boot/ota/metadata.jwt", "w") as f:
                    f.write(self._metadata_jwt)
                self._metadata = OtaMetaData(
                    self._metadata_jwt, self._get_metadata_jwt_url(), self.__header_dict
                )
            else:
                self._metadata_jwt = ""
        except Exception as e:
            self._meta_data_file = ""
            print("Error: OTA meta data download fail.: ", e)
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
                print("certificate hash missmatch: ")
                print("    dl hash: ", sha256(pem).hexdigest())
                print("    hash: ", cert_hash)
                return False
            return metadata.verify(pem)
        else:
            print("response error: ", response.status_code)
            return False
        return True

    def _mount_bank(self, bank, target_dir):
        """
        mount next bank
        """
        try:
            command_line = "mount " + bank + " " + target_dir
            if self.__verbose:
                print("commandline: " + command_line)
            subprocess.check_output(shlex.split(command_line))
        except Exception as e:
            print("Mount error!: ", e)
            return False
        return True

    def _unmount_bank(self, target_dir):
        """
        unmount bank
        """
        try:
            if pathlib.Path(target_dir).is_mount():
                command_line = "umount " + target_dir
                if self.__verbose:
                    print("commandline: " + command_line)
                subprocess.check_output(shlex.split(command_line))
        except Exception as e:
            print("Unmount error!: ", e)
            return False
        return True

    def _cleanup_dir(self, target_dir):
        """
        cleanup next bank
        """
        if self.__verbose:
            print("cleanup directory: " + target_dir)
        if target_dir == "" or target_dir == "/":
            return False
        try:
            command_line = "rm -rf " + target_dir + "/*"
            if self.__verbose:
                print("commandline: " + command_line)
            # subprocess.check_output(shlex.split(command_line))
            # proc = subprocess.call(command_line.strip().split(" "))
            proc = subprocess.call(command_line, shell=True)
        except Exception as e:
            print("rm error!:", e)
            return False
        return True

    def _cleanup_rollback_dir(self):
        """"""
        try:
            if os.path.isdir(self._rollback_dir):
                print("removedir: ", self._rollback_dir)
                shutil.rmtree(self._rollback_dir)
            if self.__verbose:
                print("makedir: ", self._rollback_dir)
            os.mkdir(self._rollback_dir)
        except Exception as e:
            print("cleanup rollback directory error!: ", e)
            return False
        return True

    def _prepare_next_bank(self, bank, target_dir):
        """
        prepare next boot bank
            mount & clean up
        """
        # mount
        if not self._mount_bank(bank, target_dir):
            return False
        # cleanup
        if not self._cleanup_dir(target_dir):
            self._unmount_bank(target_dir)
            return False
        # clean rollback dir
        if not self._cleanup_rollback_dir():
            self._unmount_bank(target_dir)
            return False
        return True

    def _download_list_file(self, url, list_file, hash=""):
        """
        Download list file(debug)
        """
        dirs_url = os.path.join(url, list_file)
        dest_path = os.path.join("/tmp", list_file)
        return self._download_raw_file_with_retry(dirs_url, dest_path, hash)

    def _gen_directories(self, dirlist_file, target_dir):
        """
        generate directories on another bank
        """
        try:
            with open(dirlist_file) as f:
                for l in f.read().splitlines():
                    # print(str(l))
                    dirinf = DirectoryInf(l)
                    # print("dir inf: ", dirinf.path)
                    # print("target dir: ", target_dir)
                    # target_path = os.path.join(target_dir, dirinf.path)
                    target_path = target_dir + dirinf.path
                    if self.__verbose:
                        print("target path: ", target_path)
                    os.makedirs(target_path, mode=int(dirinf.mode, 8))
                    os.chown(target_path, int(dirinf.uid), int(dirinf.gpid))
                    os.chmod(target_path, int(dirinf.mode, 8))
        except Exception as e:
            print("directory setup error!: ", e)
            return False
        return True

    def _setup_directories(self, target_dir):
        """
        generate directories on another bank
        """
        # get directories metadata
        dirs_file, dirs_hash = self._metadata.get_directories_info()
        dirs_url = os.path.join(self.__url, dirs_file)
        tmp_list_file = os.path.join("/tmp", dirs_file)
        if self._download_raw_file_with_retry(
            dirs_url, tmp_list_file, dirs_hash
        ):
            # generate directories
            # return self._gen_directories(tmp_list_file, target_dir)
            if self._gen_directories(tmp_list_file, target_dir):
                # move list file to rollback dir
                dest_file = os.path.join(self._rollback_dir, self._dirlist_file)
                shutil.move(tmp_list_file, dest_file)
                return True
        return False

    @staticmethod
    def exit_chroot(real_root, cwd):
        """
        exit chroot and back to cwd
        """
        # exit chroot
        os.fchdir(real_root)
        os.chroot(".")
        # Back to old root
        os.close(real_root)
        os.chdir(cwd)

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
                    if self.__verbose:
                        print("src: " + slinkf.srcpath)
                        print("slink: " + slinkf.slink)
                    if slinkf.slink.find("/boot") == 0:
                        # /boot directory
                        # exit chroot environment
                        self.exit_chroot(real_root, cwd)
                        try:
                            dest_file = ""
                            if os.path.exists(slinkf.slink):
                                dest_dir = self._rollback_dir + "/"
                                shutil.move(slinkf.slink, dest_dir)
                            os.symlink(slinkf.srcpath, slinkf.slink)
                        except Exception as e:
                            print("symbolic link error!")
                            if dest_file != "":
                                shutil.move(dest_file, slinkf.slink)
                            raise (OtaError("Cannot make symbolic link."))
                        # re-enter the chrooted environment
                        os.chroot(target_dir)
                    else:
                        # others
                        os.symlink(slinkf.srcpath, slinkf.slink)
            except Exception as e:
                print("symboliclink error:", e)
                res = False
            finally:
                # exit chroot
                self.exit_chroot(real_root, cwd)
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
        if self.__verbose:
            print("download file:", regular_url)
        return self._download_raw_file_with_retry(regular_url, target_path, hash256, fl)

    def _gen_boot_dir_file(self, rootfs_dir, target_dir, regular_inf, prev_inf, fl):
        """
        generate /boot directory file
        """
        dest_path = regular_inf.path
        if (
            int(regular_inf.links) >= 2
            and prev_inf != ""
            and prev_inf.sha256hash == regular_inf.sha256hash
        ):
            # create hard link
            if self.__verbose:
                print("links: ", regular_inf.links)
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
                if self.__verbose:
                    print("file already exist! no copy or download!")
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
                    if self.__verbose:
                        print("Download: ", regular_inf.path)
                        print("file hash: ", regular_inf.sha256hash)
                else:
                    raise OtaError("File down load error!")
                if self.__verbose:
                    print("regular_file: ", regular_inf.path)
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
            if self.__verbose:
                print("links: ", regular_inf.links)
            src_path = os.path.join(target_dir, "." + prev_inf.path)
            os.link(src_path, dest_path)
        else:
            # no hard link
            if self.__verbose:
                print("No hard links: ", regular_inf.links)
            current_file = os.path.join("/", "." + regular_inf.path)
            if (
                os.path.isfile(current_file)
                and _file_sha256(current_file) == regular_inf.sha256hash
            ):
                # copy from current bank
                if self.__verbose:
                    print("copy from current: ", current_file)
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
                    if self.__verbose:
                        print("Download: ", regular_inf.path)
                        print("file hash: ", regular_inf.sha256hash)
                else:
                    raise OtaError("File down load error!")
            if self.__verbose:
                print("regular_file: ", dest_path)
                print("permissoin: ", str(regular_inf.mode))
            os.chown(dest_path, int(regular_inf.uid), int(regular_inf.gpid))
            os.chmod(dest_path, int(regular_inf.mode, 8))

    def _sort_list(self, regulars_file="./tests/regulars.txt"):
        """"""
        with open(regulars_file) as f:
            rlist = []
            for l in f.readlines():
                rlist.append(l.replace("\n", "").split(","))
            sorted_list = sorted(rlist, key=itemgetter(4))
            with open("./tests/sorted_regular_list.txt", "w") as fdst:
                for lst in sorted_list:
                    fdst.write(str(lst) + "\n")
        return False

    def _gen_regular_files(self, rootfs_dir, regulars_file, target_dir):
        """
        generate regular files
        """
        with tempfile.NamedTemporaryFile(delete=False) as flog:
            log_file = flog.name
            with open(flog.name, "w") as fl:
                res = True
                with open(regulars_file) as f:
                    try:
                        cwd = os.getcwd()
                        os.chdir(target_dir)
                        if self.__verbose:
                            print("target_dir: ", target_dir)
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
                            if self.__verbose:
                                print(
                                    "file: ",
                                    regular_inf.path,
                                    "hash: ",
                                    regular_inf.sha256hash,
                                )
                            if regular_inf.path.find("/boot/") == 0:
                                # /boot directory file
                                if self.__verbose:
                                    print("boot file: ", regular_inf.path)
                                self._gen_boot_dir_file(
                                    rootfs_dir, target_dir, regular_inf, prev_inf, fl
                                )
                            else:
                                # others
                                if self.__verbose:
                                    print("no boot file: ", regular_inf.path)
                                self._gen_regular_file(
                                    rootfs_dir, target_dir, regular_inf, prev_inf, fl
                                )
                            prev_inf = regular_inf
                    except Exception as e:
                        print("regular files setup error!: ", e)
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
        return False

    def _gen_persistent_files(self, list_file, target_dir):
        """
        generate persistent files
        """
        res = True
        verbose_back = self.__verbose
        self.__verbose = True
        with open(list_file, mode="r") as f:
            try:
                for l in f.read().splitlines():
                    persistent_info = PersistentInf(l)
                    src_path = persistent_info.path
                    if src_path.find("/boot") == 0:
                        # /boot directory
                        print("do nothing for boot dir file: ", src_path)
                    else:
                        # others
                        if src_path[0] == "/":
                            dest_path = os.path.join(target_dir, "." + src_path)
                        else:
                            dest_path = os.path.join(target_dir, src_path)
                        if os.path.exists(src_path):
                            if os.path.isdir(src_path):
                                if os.path.exists(dest_path):
                                    if self.__verbose:
                                        print("rmtree: ", dest_path)
                                    shutil.rmtree(dest_path)
                                if self.__verbose:
                                    print(
                                        "persistent dir copy: ",
                                        src_path,
                                        " -> ",
                                        dest_path,
                                    )
                                # shutil.copytree(src_path, dest_path, symlinks=True)
                                _copytree_complete(src_path, dest_path, symlinks=True)
                            else:
                                if self.__verbose:
                                    print(
                                        "persistent file copy: ",
                                        src_path,
                                        " -> ",
                                        dest_path,
                                    )
                                if os.path.exists(dest_path):
                                    print("rm file: ", dest_path)
                                    os.remove(dest_path)
                                _copy_complete(src_path, dest_path)
                        else:
                            print("persistent file not exist: ", src_path)
            except Exception as e:
                print("persistent file error:", e)
                res = False
        self.__verbose = verbose_back
        return res

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
                print("persistent file download error: ", persistent_url)
                return False
            else:
                print("persistent file download success: ", persistent_url)
        else:
            # read list from the local file
            tmp_list_file = self._persistentlist_file

        if self._gen_persistent_files(tmp_list_file, target_dir):
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
            print("file not exist: ", fstab_file)
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
                                if self.__verbose:
                                    print("Already set to next bank!")
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
        print("Update error!: " + str(error))
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
            print("Load update files error: ", e)
            return False
        return True

    @staticmethod
    def header_str_to_dict(header_str):
        """"""
        header_dict = {}
        for l in header_str.split(","):
            kl = l.split(":")
            if len(kl) == 2:
                header_dict[kl[0]] = kl[1]
        return header_dict

    def _set_update_ecuinfo(self, update_info):
        """"""
        print("_update_ecu_info start")
        ecuinfo = update_info.ecu_info
        print("[ecu_info] ", ecuinfo)
        ecu_found = False
        if ecuinfo.ecu_id == self.__update_ecu_info["main_ecu"]["ecu_id"]:
            print("ecu_id matched!")
            self.__update_ecu_info["main_ecu"]["ecu_name"] = ecuinfo.ecu_name
            self.__update_ecu_info["main_ecu"]["ecu_type"] = ecuinfo.ecu_type
            self.__update_ecu_info["main_ecu"]["version"] = ecuinfo.version
            self.__update_ecu_info["main_ecu"]["independent"] = ecuinfo.independent
            ecu_found = True
            print("__update_ecu_info: ", self.__update_ecu_info)
        else:
            print("ecu_id not matched!")
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

        print("_update_ecu_info end")
        return ecu_found

    def _save_update_ecuinfo(self):
        """"""
        output_file = self.__update_ecuinfo_yaml_file
        print("output_file: ", output_file)
        with tempfile.NamedTemporaryFile("w", delete=False) as ftmp:
            tmp_file_name = ftmp.name
            with open(tmp_file_name, "w") as f:
                f.write(yaml.dump(self.__update_ecu_info))
                f.flush()
        shutil.move(tmp_file_name, output_file)
        os.sync()
        return True

    def _update(self, ecu_update_info, reboot=True):
        """
        OTA update execution
        """
        # -----------------------------------------------------------
        # set 'UPDATE' state
        self._ota_status.set_ota_status("UPDATE")
        print(ecu_update_info)
        self.__url = ecu_update_info.url
        metadata = ecu_update_info.metadata
        metadata_jwt_url = os.path.join(self.__url, metadata)
        self.__header_dict = self.header_str_to_dict(ecu_update_info.header)
        # print("metadata_jwt: ", metadata_jwt_url)
        # print(header_dict)
        # print(self.__cookie)

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
            self._unmount_bank(self._mount_point)
            return False
        #
        # -----------------------------------------------------------
        # set 'PREPARED' state
        self._ota_status.set_ota_status("PREPARED")
        if reboot:
            # switch reboot
            if not self._grub_ctl.prepare_grub_switching_reboot():
                # inform error
                self._inform_update_error("Switching bank failed!")
                # set 'NORMAL' state
                self._ota_status.set_ota_status("NORMAL")
                self._unmount_bank(self._mount_point)
                return False
            #
            # -----------------------------------------------------------
            # set 'SWITCHA/SWITCHB' state
            next_state = self._get_switch_status_for_reboot(next_bank)
            self._ota_status.set_ota_status(next_state)
            #
            # reboot
            #
            self._grub_ctl.reboot()

        return True

    @staticmethod
    def _find_ecu_info(ecuupdateinfo_list, ecu_id):
        """"""
        ecu_info = {}
        for ecuupdateinfo in ecuupdateinfo_list:
            if ecu_id == ecuupdateinfo.ecu_info.ecu_id:
                ecu_info = ecuupdateinfo
        return ecu_info

    def _reboot(self):
        if self._ota_status.get_ota_status() == "PREPARED":
            # switch reboot
            if not self._grub_ctl.prepare_grub_switching_reboot():
                # inform error
                self._inform_update_error("Switching bank failed!")
                # set 'NORMAL' state
                self._ota_status.set_ota_status("NORMAL")
                self._unmount_bank(self._mount_point)
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

    def _rollback(self):
        """"""
        if self._ota_status.get_ota_status() == "NORMAL":
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
            print("No available rollback.")
            return False
        return True
