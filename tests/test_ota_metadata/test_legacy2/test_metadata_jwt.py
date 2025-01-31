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
"""
NOTE: basically copied from test_legacy.py
"""


from __future__ import annotations

import pytest

from ota_metadata.legacy2.metadata_jwt import MetadataJWTClaimsLayout, MetaFile

# --- valid metadata.jwt --- #
# original layout
PAYLOAD = """\
[\
{"version": 1}, \
{"directory": "dirs.txt", "hash": "43afbd19eab7c9e27f402a3332c38d072a69c7932fb35c32c1fc7069695235f1"}, \
{"symboliclink": "symlinks.txt", "hash": "6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d"}, \
{"regular": "regulars.txt", "hash": "a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600"}, \
{"persistent": "persistents.txt", "hash": "3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3"}, \
{"rootfs_directory": "data"}, \
{"certificate": "certificate.pem", "hash": "24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2"}\
]\
"""
PAYLOAD_PARSED = MetadataJWTClaimsLayout(
    version=1,
    directory=MetaFile(
        file="dirs.txt",
        hash="43afbd19eab7c9e27f402a3332c38d072a69c7932fb35c32c1fc7069695235f1",
    ),
    symboliclink=MetaFile(
        file="symlinks.txt",
        hash="6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d",
    ),
    regular=MetaFile(
        file="regulars.txt",
        hash="a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600",
    ),
    persistent=MetaFile(
        file="persistents.txt",
        hash="3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3",
    ),
    rootfs_directory="data",
    certificate=MetaFile(
        file="certificate.pem",
        hash="24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2",
    ),
)

# revision1: add total_regular_size
PAYLOAD_W_TOTAL_SIZE = """\
[\
{"version": 1}, \
{"directory": "dirs.txt", "hash": "43afbd19eab7c9e27f402a3332c38d072a69c7932fb35c32c1fc7069695235f1"}, \
{"symboliclink": "symlinks.txt", "hash": "6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d"}, \
{"regular": "regulars.txt", "hash": "a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600"}, \
{"persistent": "persistents.txt", "hash": "3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3"}, \
{"rootfs_directory": "data"}, \
{"certificate": "certificate.pem", "hash": "24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2"}, \
{"total_regular_size": "108708870"}\
]\
"""
PAYLOAD_W_TOTAL_SIZE_PARSED = MetadataJWTClaimsLayout(
    version=1,
    directory=MetaFile(
        file="dirs.txt",
        hash="43afbd19eab7c9e27f402a3332c38d072a69c7932fb35c32c1fc7069695235f1",
    ),
    symboliclink=MetaFile(
        file="symlinks.txt",
        hash="6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d",
    ),
    regular=MetaFile(
        file="regulars.txt",
        hash="a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600",
    ),
    persistent=MetaFile(
        file="persistents.txt",
        hash="3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3",
    ),
    rootfs_directory="data",
    certificate=MetaFile(
        file="certificate.pem",
        hash="24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2",
    ),
    total_regular_size=108708870,
)

# revision2: add compressed_rootfs
PAYLOAD_W_COMPRESSED_ROOTFS = """\
[\
{"version": 1}, \
{"directory": "dirs.txt", "hash": "43afbd19eab7c9e27f402a3332c38d072a69c7932fb35c32c1fc7069695235f1"}, \
{"symboliclink": "symlinks.txt", "hash": "6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d"}, \
{"regular": "regulars.txt", "hash": "a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600"}, \
{"persistent": "persistents.txt", "hash": "3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3"}, \
{"rootfs_directory": "data"}, \
{"certificate": "certificate.pem", "hash": "24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2"}, \
{"total_regular_size": "108708870"},\
{"compressed_rootfs_directory": "data.zstd"}\
]\
"""
PAYLOAD_W_COMPRESSED_ROOTFS_PARSED = MetadataJWTClaimsLayout(
    version=1,
    directory=MetaFile(
        file="dirs.txt",
        hash="43afbd19eab7c9e27f402a3332c38d072a69c7932fb35c32c1fc7069695235f1",
    ),
    symboliclink=MetaFile(
        file="symlinks.txt",
        hash="6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d",
    ),
    regular=MetaFile(
        file="regulars.txt",
        hash="a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600",
    ),
    persistent=MetaFile(
        file="persistents.txt",
        hash="3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3",
    ),
    rootfs_directory="data",
    certificate=MetaFile(
        file="certificate.pem",
        hash="24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2",
    ),
    total_regular_size=108708870,
    compressed_rootfs_directory="data.zstd",
)

# --- invalid metadata.jwt --- #
PAYLOAD_MISSING_MUST_SET_FIELD_DIRECTORY = """\
[\
{"version": 1}, \
{"symboliclink": "symlinks.txt", "hash": "6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d"}, \
{"regular": "regulars.txt", "hash": "a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600"}, \
{"persistent": "persistents.txt", "hash": "3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3"}, \
{"rootfs_directory": "data"}, \
{"certificate": "ota-intermediate.pem", "hash": "24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2"}, \
{"total_regular_size": "108708870"},\
{"compressed_rootfs_directory": "data.zstd"}\
]\
"""

PAYLOAD_MISSING_MUST_SET_FIELD_ROOTFS = """\
[\
{"version": 1}, \
{"directory": "dirs.txt", "hash": "43afbd19eab7c9e27f402a3332c38d072a69c7932fb35c32c1fc7069695235f1"}, \
{"symboliclink": "symlinks.txt", "hash": "6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d"}, \
{"regular": "regulars.txt", "hash": "a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600"}, \
{"persistent": "persistents.txt", "hash": "3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3"}, \
{"certificate": "ota-intermediate.pem", "hash": "24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2"}, \
{"total_regular_size": "108708870"},\
{"compressed_rootfs_directory": "data.zstd"}\
]\
"""


@pytest.mark.parametrize(
    "_input, _expected",
    (
        (PAYLOAD, PAYLOAD_PARSED),
        (PAYLOAD_W_TOTAL_SIZE, PAYLOAD_W_TOTAL_SIZE_PARSED),
        (PAYLOAD_W_COMPRESSED_ROOTFS, PAYLOAD_W_COMPRESSED_ROOTFS_PARSED),
        # invalid metadata.jwt
        (PAYLOAD_MISSING_MUST_SET_FIELD_DIRECTORY, None),
        (PAYLOAD_MISSING_MUST_SET_FIELD_ROOTFS, None),
    ),
)
def test_parse_raw_jwt(_input, _expected) -> None:
    try:
        assert MetadataJWTClaimsLayout.parse_payload(_input) == _expected
    except Exception as e:
        assert _expected is None, f"expecting {_input} is valid, but: {e!r}"
