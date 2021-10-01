#!/usr/bin/env python3

import os
from hashlib import sha256
import base64
import json
from OpenSSL import crypto
from pathlib import Path
import glob
import re
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

    """
    The root and intermediate certificates exist under certs_dir.
    When A.root.pem, A.intermediate.pem, B.root.pem, and B.intermediate.pem
    exist, the groups of A* and the groups of B* are handled as a chained
    certificate.
    verify function verifies specified certificate with them.
    Certificates file name format should be: '.*\..*.pem'
    NOTE:
    If there is no root or intermediate certificate, certification verification
    is not performed.
    """
    CERTS_DIR = Path(__file__).parent / "certs"

    def __init__(self, ota_metadata_jwt):
        """
        OTA metadata parser
            url : metadata server URL
            cookie : signed cookie
            metadata_jwt : metadata JWT file name
        """
        self.__metadata_jwt = ota_metadata_jwt
        self.__metadata_dict = self._parse_metadata(ota_metadata_jwt)
        self._certs_dir = OtaMetadata.CERTS_DIR

    def verify(self, certificate: str):
        """"""
        # verify certificate itself before hand.
        self._verify_certificate(certificate)

        # verify metadata.jwt
        try:
            cert = crypto.load_certificate(crypto.FILETYPE_PEM, certificate)
            verify_data = self._get_header_payload().encode()
            logger.debug(f"verify data: {verify_data}")
            crypto.verify(cert, self._signature, verify_data, "sha256")
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

    def _verify_certificate(self, certificate: str):
        certs = [cert.name for cert in sorted(list(self._certs_dir.glob("*.*.pem")))]
        if len(certs) == 0:
            logger.warning("there is no root or intermediate certificate")
            return
        # e.g. certs == [A.1.pem, A.2.pem, B.1.pem, B.2.pem]
        prefixes = set()
        for cert in certs:
            m = re.match(r"(.*)\..*.pem", cert)
            prefixes.add(m.group(1))
        logger.info(f"certs prefixes {prefixes}")

        def _load_certificates(store, certs):
            for cert in certs:
                logger.info(f"cert {cert}")
                c = crypto.load_certificate(crypto.FILETYPE_PEM, open(cert).read())
                store.add_cert(c)

        for prefix in sorted(prefixes):  # use sorted to fix the order of verification
            store = crypto.X509Store()
            _load_certificates(store, self._certs_dir.glob(f"{prefix}.*.pem"))
            try:
                cert_to_verify = crypto.load_certificate(
                    crypto.FILETYPE_PEM, certificate
                )
            except crypto.Error as e:
                raise OtaErrorRecoverable(f"invalid certificate {certificate}")

            try:
                store_ctx = crypto.X509StoreContext(store, cert_to_verify)
                store_ctx.verify_certificate()
                logger.info("certificate verify: OK")
                return
            except crypto.X509StoreContextError as e:
                logger.warning(f"NOTE: This warning can be ignored {e}")

        raise OtaErrorRecoverable(f"certificate {certificate} could not be verified")
