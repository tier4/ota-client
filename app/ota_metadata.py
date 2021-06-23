#!/usr/bin/env python3

import os
from hashlib import sha256
import base64
import json
from OpenSSL import crypto

from logging import getLogger, INFO, DEBUG

logger = getLogger(__name__)
logger.setLevel(INFO)

class OtaMetaData:
    """
    OTA Metadata Class
    """

    def __init__(self, ota_metadata_jwt):
        """
        OTA metadata parser
            url : metadata server URL
            cookie : signed cookie
            metadata_jwt : metadata JWT file name
        """
        self._enable_persistent = True
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
        self.__payload = self._parse_metadata(ota_metadata_jwt)
        logger.debug(f"payload: {self.__payload}")

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
        logger.debug(f"JWT header: {header_json}")
        payload_json = base64.urlsafe_b64decode(jwt_list[1]).decode()
        logger.debug(f"JWT payload: {payload_json}")
        logger.debug(f"JWT signature raw: {jwt_list[2]}")
        signature = base64.urlsafe_b64decode(jwt_list[2])
        logger.debug(f"JWT signature: {signature}")

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
            pemCert = crypto.load_certificate(crypto.FILETYPE_PEM, buffer)
            # get public key
            self.__public_key = crypto.dump_publickey(
                crypto.FILETYPE_PEM, pemCert.get_pubkey()
            )

        return self.__public_key

    def _parse_payload(self, payload_json):
        """
        Parse payload json file
        """
        try:
            # read payload json
            payload = json.loads(payload_json)
            logger.debug(f"payload: {payload}")
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
                    logger.error(f"version error! version: {self.version}")
                    return False
            else:
                logger.error("json load error!")
                return False
        except Exception as e:
            logger.exception("payload read error:")
            return False
        return True

    def _parse_metadata(self, metadata_jwt):
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
            logger.debug("perse payload success!")
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
            logger.debug(f"certificate: {certificate}")
            # publick_key = crypto.load_publickey(crypto.FILETYPE_PEM, certificate_pem)
            verify_data = self.get_header_payload().encode()
            logger.debug(f"verify data: {verify_data}")
            crypto.verify(certificate, self._signature, verify_data, "sha256")
            return True
        except Exception as e:
            logger.exception("Verify error:")
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
