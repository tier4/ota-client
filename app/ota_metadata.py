#!/usr/bin/env python3

import os
import shlex
import shutil
from hashlib import sha256
import pathlib
import filecmp
import subprocess
import getpass
import base64
import json
import jwt
from OpenSSL import crypto


class OtaMetaData:
    """
    OTA Metadata Class
    """

    def __init__(self, ota_metadata_jwt, url, cookie):
        """
        OTA metadata parser
            url : metadata server URL
            cookie : signed cookie
            metadata_jwt : metadata JWT file name
        """
        self.__verbose = False
        self._enable_persistent = True
        self.__url = url
        self.__cookie = cookie
        self.__metadata_jwt = ota_metadata_jwt
        self.__public_key = ""
        self.__version = 0
        self.__directory = ""
        self.__directory_hash = ""
        self.__symboliclink = ""
        self.__symboliclink_hash = ""
        self.__regular = ""
        self.__regular_hash = ""
        self.__persistent = ""
        self.__persistent_hash = ""
        self.__rootfs_directory = "data"
        self.__certificate = ""
        self.__certificate_hash = ""
        self.__payload = self._parse_metadata(ota_metadata_jwt, url, cookie)
        print("payload: ", self.__payload)

    @staticmethod
    def _file_sha256(filename):
        with open(filename, "rb") as f:
            digest = sha256(f.read()).hexdigest()
            return digest

    @staticmethod
    def _is_regular(path):
        return os.path.isfile(path) and not os.path.islink(path)

    @staticmethod
    def _path_stat(base, path):
        return os.lstat(os.path.join(base, path))

    def _jwt_decode_no_verify(self, jwt):
        """
        JWT decode
            return payload.json
        """
        jwt_list = jwt.split(".")

        header_json = base64.urlsafe_b64decode(jwt_list[0]).decode()
        if self.__verbose:
            print("JWT header: " + header_json)
        payload_json = base64.urlsafe_b64decode(jwt_list[1]).decode()
        if self.__verbose:
            print("JWT payload: " + payload_json)
            print("JWT signature raw: ", jwt_list[2])
        signature = base64.urlsafe_b64decode(jwt_list[2])
        if self.__verbose:
            print("JWT signature: ", str(signature))

        return header_json, payload_json, signature

    @staticmethod
    def _jwt_verify(metadata_jwt, pub_key):
        """
        verify metadata.jwt
        """
        # ToDO: verify implementation
        return True

    def _get_public_key(self, url, file_name):
        """
        get public key from downloaded certificate.pem file
        """
        # download certificate file
        certificate_url = url + "/" + file_name
        pem_file = os.path.join("/tmp", file_name)
        self._download_file(certificate_url, pem_file)

        # read
        with open(pem_file, "rb") as f:
            # byte conversion
            buffer = f.read()
            # read certificate
            pemCert = crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM, buffer)
            # get public key
            self.__public_key = crypto.dump_publickey(
                OpenSSL.crypto.FILETYPE_PEM, pemCert.get_pubkey()
            )

        return self.__public_key

    def _parse_payload(self, payload_json):
        """
        Parse payload json file
        """
        try:
            # read payload json
            payload = json.loads(payload_json)
            print("payload: ", payload)
            if payload:
                self.__version = payload[0]["version"]
                if self.__version == 1:
                    self.__directory = payload[1]["directory"]
                    self.__directory_hash = payload[1]["hash"]
                    self.__symboliclink = payload[2]["symboliclink"]
                    self.__symboliclink_hash = payload[2]["hash"]
                    self.__regular = payload[3]["regular"]
                    self.__regular_hash = payload[3]["hash"]
                    if self._enable_persistent:
                        self.__persistent = payload[4]["persistent"]
                        self.__persistent_hash = payload[4]["hash"]
                        self.__rootfs_directory = payload[5]["rootfs_directory"]
                    self.__certificate = payload[6]["certificate"]
                    self.__certificate_hash = payload[6]["hash"]

                else:
                    print("version error! version: " + str(self.version))
            else:
                print("json load error!")
                return False
        except Exception as e:
            print("payload read error:", e)
            return False
        return True

    def _parse_metadata(self, metadata_jwt, url, cookie):
        """
        Parse metadata.jwt
        """
        # payload_json = jwt.decode(metadata_jwt, options={"verify_signature": False})
        (
            self._header_json,
            self._payload_json,
            self._signature,
        ) = self._jwt_decode_no_verify(metadata_jwt)
        #
        if self._parse_payload(self._payload_json):
            if self.__verbose:
                print("perse payload success!")
            # verify
            # if self._jwt_verify(metadata_jwt, self._get_public_key(url, cookie)):
            #    raise(Exception, "JWT verify error!")

    def process_metadata(self, metadata_jwt_name, url, cookie):
        """
        process metadata
        """
        # download metadata.jwt
        metadata_url = url + "/" + metadata_jwt_name
        metadata_jwt_file = os.path.join("/tmp/", metadata_jwt_name)
        self._download_file(metadata_url, metadata_jwt_file, cookie)

        if os.path.isfile(metadata_jwt_file):
            self._parse_metadata(metadata_jwt_file, url, cookie)

    def verify(self, certificate_pem):
        """"""
        try:
            # signature = self.get_signature()
            certificate = crypto.load_certificate(crypto.FILETYPE_PEM, certificate_pem)
            if self.__verbose:
                print("certificate: ", certificate)
            # publick_key = crypto.load_publickey(crypto.FILETYPE_PEM, certificate_pem)
            verify_data = self.get_header_payload().encode()
            if self.__verbose:
                print("verify data: ", verify_data)
            crypto.verify(certificate, self._signature, verify_data, "sha256")
            return True
        except Exception as e:
            print("Verify error: ", e)
            return False

    def get_signature(self):
        """
        Get signature
        """
        return self._signature

    def get_header_payload(self):
        """
        Get Header and Payload urlsafe base64 string
        """
        jwt_list = self.__metadata_jwt.split(".")
        return jwt_list[0] + "." + jwt_list[1]

    def get_directories_info(self):
        """
        return
            directory file path name
            directory file hash
        """
        return self.__directory, self.__directory_hash

    def get_symboliclinks_info(self):
        """
        return
            symboliclink file path name
            symboliclink file hash
        """
        return self.__symboliclink, self.__symboliclink_hash

    def get_regulars_info(self):
        """
        return
            regular file path name
            regular file hash
        """
        return self.__regular, self.__regular_hash

    def get_persistent_info(self):
        """
        return
            persistent file list path name
            persistent file list hash
        """
        return self.__persistent, self.__persistent_hash

    def get_rootfsdir_info(self):
        """
        return
            rootfs_directory path name
        """
        return self.__rootfs_directory

    def get_certificate_info(self):
        """
        return
            certificate file path name
            certificate file hash
        """
        return self.__certificate, self.__certificate_hash

    def is_persistent_enabled(self):
        return self._enable_persistent
