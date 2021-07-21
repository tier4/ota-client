#!/usr/bin/env python3

import re
import pytest

# TODO: metadata.jwt should be generated in the test.
METADATA_JWT = """\
eyJhbGciOiAiRVMyNTYifQ==.W3sidmVyc2lvbiI6IDF9LCB7ImRpcmVjdG9yeSI6ICJkaXJzLnR4dCIsICJoYXNoIjogIjQzYWZiZDE5ZWFiN2M5ZTI3ZjQwMmEzMzMyYzM4ZDA3MmE2OWM3OTMyZmIzNWMzMmMxZmM3MDY5Njk1MjM1ZjEifSwgeyJzeW1ib2xpY2xpbmsiOiAic3ltbGlua3MudHh0IiwgImhhc2giOiAiNjY0M2JmODk2ZDNhYzNiZDQwMzRkNzQyZmFlMGQ3ZWI4MmJkMzg0MDYyNDkyMjM1NDA0MTE0YWViMzRlZmQ3ZCJ9LCB7InJlZ3VsYXIiOiAicmVndWxhcnMudHh0IiwgImhhc2giOiAiYTM5MGY5MmZlNDliODQwMmEyYmI5ZjBiNTk0ZTlkZTcwZjcwZGM2YmY0MjkwMzFhYzRkMGIyMTM2NTI1MTYwMCJ9LCB7InBlcnNpc3RlbnQiOiAicGVyc2lzdGVudHMudHh0IiwgImhhc2giOiAiMzE5NWRlZDczMDQ3NGQwMTgxMjU3MjA0YmEwZmQ3OTc2NjcyMWFiNjJhY2UzOTVmMjBkZWNkNDQ5ODNjYjJkMyJ9LCB7InJvb3Rmc19kaXJlY3RvcnkiOiAiZGF0YSJ9LCB7ImNlcnRpZmljYXRlIjogIm90YS1pbnRlcm1lZGlhdGUucGVtIiwgImhhc2giOiAiMjRjMGM5ZWEyOTI0NTgzOThmMDViOWIyYTMxYjQ4M2M0NWQyN2UyODQ3NDNhM2YwYzc5NjNlMmFjMGM2MmVkMiJ9XQ==.MEYCIQC2iI5Tu5FxG_Int-rlsPDw37rZKR6QnOC51droeNN1uwIhAKBKhbvgFx_Ja8-cpIZIncykwlzGI6_bFKVbY2VmV1qt
"""
HEADER = """\
{"alg": "ES256"}\
"""

PAYLOAD = """\
[\
{"version": 1}, \
{"directory": "dirs.txt", "hash": "43afbd19eab7c9e27f402a3332c38d072a69c7932fb35c32c1fc7069695235f1"}, \
{"symboliclink": "symlinks.txt", "hash": "6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d"}, \
{"regular": "regulars.txt", "hash": "a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600"}, \
{"persistent": "persistents.txt", "hash": "3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3"}, \
{"rootfs_directory": "data"}, \
{"certificate": "ota-intermediate.pem", "hash": "24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2"}\
]\
"""

PEM_DATA = """\
-----BEGIN CERTIFICATE-----
MIIB8jCCAZigAwIBAgIUMBz3132x+ivLKBCN1B+MYndtr5UwCgYIKoZIzj0EAwIw
STELMAkGA1UEBhMCSlAxDjAMBgNVBAgMBVRva3lvMQ4wDAYDVQQKDAVUaWVyNDEa
MBgGA1UEAwwRb3RhLXJvb3QudGllcjQuanAwHhcNMjEwNDAyMDEwNTAxWhcNNDEw
NDAyMDEwNTAxWjBRMQswCQYDVQQGEwJKUDEOMAwGA1UECAwFVG9reW8xDjAMBgNV
BAoMBVRpZXI0MSIwIAYDVQQDDBlvdGEtaW50ZXJtZWRpYXRlLnRpZXI0LmpwMFkw
EwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEmeDPmxADna5CQkGbluX9Urvp6IHrgy8v
bwoYvSNivTbfqfWqiapPj4Mqdng5fTKkcfbpCWmVPk0b/hYCXkGcpaNWMFQwHQYD
VR0OBBYEFAvQPuf+TI0ZKTwL/qcdxj8QJI/pMB8GA1UdIwQYMBaAFJ31n7r0ZqSQ
3IJ/cU9EaFArVLN4MBIGA1UdEwEB/wQIMAYBAf8CAQAwCgYIKoZIzj0EAwIDSAAw
RQIhAK1P0L0V2INYAIUhUKeHLFO7q3uXoDUH9NxY+Hmtwh0XAiByLC92vxrX5hgJ
cuJkCl2Q1K+GfNI17pmMORuNJDODCQ==
-----END CERTIFICATE-----

"""

DIRS_FNAME = "dirs.txt"
DIRS_HASH = "43afbd19eab7c9e27f402a3332c38d072a69c7932fb35c32c1fc7069695235f1"

SYMLINKS_FNAME = "symlinks.txt"
SYMLINKS_HASH = "6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d"

REGULAR_FNAME = "regulars.txt"
REGULAR_HASH = "a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600"

PERSISTENT_FNAME = "persistents.txt"
PERSISTENT_HASH = "3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3"

ROOTFS_DIR = "data"

CERTIFICATE_FNAME = "ota-intermediate.pem"
CERTIFICATE_HASH = "24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2"


SIGNATURE = b"0F\x02!\x00\xb6\x88\x8eS\xbb\x91q\x1b\xf2'\xb7\xea\xe5\xb0\xf0\xf0\xdf\xba\xd9)\x1e\x90\x9c\xe0\xb9\xd5\xda\xe8x\xd3u\xbb\x02!\x00\xa0J\x85\xbb\xe0\x17\x1f\xc9k\xcf\x9c\xa4\x86H\x9d\xcc\xa4\xc2\\\xc6#\xaf\xdb\x14\xa5[cefWZ\xad"


def test__jwt_decode():
    from ota_metadata import OtaMetaData

    header, payload, signature = OtaMetaData._jwt_decode(METADATA_JWT)
    print(f"header: {header}")
    print(f"payload: {payload}")
    print(f"signature: {signature}")
    assert header == HEADER
    assert payload == PAYLOAD
    assert signature == SIGNATURE


def test_get_directorys_info():
    from ota_metadata import OtaMetaData

    otametadata = OtaMetaData(METADATA_JWT)
    dirs = otametadata.get_directories_info()
    assert dirs["file"] == DIRS_FNAME
    assert dirs["hash"] == DIRS_HASH


def test_get_symboliclinks_info():
    from ota_metadata import OtaMetaData

    otametadata = OtaMetaData(METADATA_JWT)
    symboliclink = otametadata.get_symboliclinks_info()
    assert symboliclink["file"] == SYMLINKS_FNAME
    assert symboliclink["hash"] == SYMLINKS_HASH


def test_get_regulars_info():
    from ota_metadata import OtaMetaData

    otametadata = OtaMetaData(METADATA_JWT)
    regular = otametadata.get_regulars_info()
    assert regular["file"] == REGULAR_FNAME
    assert regular["hash"] == REGULAR_HASH


def test_get_persistent_info():
    from ota_metadata import OtaMetaData

    otametadata = OtaMetaData(METADATA_JWT)
    persistent = otametadata.get_persistent_info()
    assert persistent["file"] == PERSISTENT_FNAME
    assert persistent["hash"] == PERSISTENT_HASH


def test_get_rootfsdir_info():
    from ota_metadata import OtaMetaData

    otametadata = OtaMetaData(METADATA_JWT)
    rootfs_dir = otametadata.get_rootfsdir_info()
    assert rootfs_dir == ROOTFS_DIR


def test_get_certificate_info():
    from ota_metadata import OtaMetaData

    otametadata = OtaMetaData(METADATA_JWT)
    certificate = otametadata.get_certificate_info()
    assert certificate["file"] == CERTIFICATE_FNAME
    assert certificate["hash"] == CERTIFICATE_HASH


def test_verify():
    from ota_metadata import OtaMetaData

    otametadata = OtaMetaData(METADATA_JWT)
    assert otametadata.verify(PEM_DATA) == True
