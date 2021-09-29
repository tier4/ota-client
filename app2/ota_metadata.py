#!/usr/bin/env python3

import os
from hashlib import sha256
import base64
import json
from OpenSSL import crypto
from logging import getLogger

from ota_error import OtaErrorUnrecoverable, OtaErrorRecoverable
import configs as cfg

from logging import getLogger, INFO, DEBUG

logger = getLogger(__name__)
logger.setLevel(cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL))


class OtaMetadata:
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
        self.__metadata_jwt = ota_metadata_jwt
        self.__metadata_dict = self._parse_metadata(ota_metadata_jwt)

    def verify(self, certificate_pem):
        """"""
        try:
            certificate = crypto.load_certificate(crypto.FILETYPE_PEM, certificate_pem)
            logger.debug(f"certificate: {certificate}")
            verify_data = self._get_header_payload().encode()
            logger.debug(f"verify data: {verify_data}")
            crypto.verify(certificate, self._signature, verify_data, "sha256")
        except Exception as e:
            raise OtaErrorRecoverable(e)

    def get_directories_info(self):
        """
        return
            directory file info list: { "file": file name, "hash": file hash }
        """
        return self.__metadata_dict["directory"]

    def get_symboliclinks_info(self):
        """
        return
            symboliclink file info: { "file": file name, "hash": file hash }
        """
        return self.__metadata_dict["symboliclink"]

    def get_regulars_info(self):
        """
        return
            regular file info: { "file": file name, "hash": file hash }
        """
        return self.__metadata_dict["regular"]

    def get_persistent_info(self):
        """
        return
            persistent file info: { "file": file name, "hash": file hash }
        """
        return self.__metadata_dict["persistent"]

    def get_rootfsdir_info(self):
        """
        return
            rootfs_directory info: {"file": dir name }
        """
        return self.__metadata_dict["rootfs_directory"]

    def get_certificate_info(self):
        """
        return
            certificate file info: { "file": file name, "hash": file hash }
        """
        return self.__metadata_dict["certificate"]

    """ private functions from here """

    def _jwt_decode(self, jwt):
        """
        JWT decode
            return payload.json
        """
        jwt_list = jwt.split(".")

        header_json = base64.urlsafe_b64decode(jwt_list[0]).decode()
        logger.debug(f"JWT header: {header_json}")
        payload_json = base64.urlsafe_b64decode(jwt_list[1]).decode()
        logger.debug(f"JWT payload: {payload_json}")
        signature = base64.urlsafe_b64decode(jwt_list[2])
        logger.debug(f"JWT signature: {signature}")

        return header_json, payload_json, signature

    def _parse_payload(self, payload_json):
        """
        Parse payload json file
        """
        keys_version_1 = {
            "directory",
            "symboliclink",
            "regular",
            "certificate",
            "persistent",
            "rootfs_directory",
        }
        hash_key = "hash"
        payload = json.loads(payload_json)
        payload_dict = {
            "version": list(entry.values())[0]
            for entry in payload
            if entry.get("version")
        }

        if payload_dict["version"] == 1:
            keys_version = keys_version_1
        for entry in payload:
            for key in keys_version:
                if key in entry.keys():
                    payload_dict[key] = {}
                    payload_dict[key]["file"] = entry.get(key)
                    if hash_key in entry:
                        payload_dict[key][hash_key] = entry.get(hash_key)
        return payload_dict

    def _parse_metadata(self, metadata_jwt):
        """
        Parse metadata.jwt
        """
        (
            self._header_json,
            self._payload_json,
            self._signature,
        ) = self._jwt_decode(metadata_jwt)
        # parse metadata.jwt payload
        return self._parse_payload(self._payload_json)

    def _get_header_payload(self):
        """
        Get Header and Payload urlsafe base64 string
        """
        jwt_list = self.__metadata_jwt.split(".")
        return jwt_list[0] + "." + jwt_list[1]
