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

from typing import Optional, TypedDict

from pydantic import SkipValidation
from simple_sqlite3_orm import (
    ConstrainRepr,
    CreateIndexParams,
    CreateTableParams,
    ORMBase,
    ORMThreadPoolBase,
    TableSpec,
)
from typing_extensions import Annotated

from ota_metadata.file_table import (
    FT_DIR_TABLE_NAME,
    FT_INODE_TABLE_NAME,
    FT_NON_REGULAR_TABLE_NAME,
    FT_REGULAR_TABLE_NAME,
    FT_RESOURCE_TABLE_NAME,
)
from otaclient_common._logging import get_burst_suppressed_logger

burst_suppressed_logger = get_burst_suppressed_logger(f"{__name__}.file_op_failed")

# ------ inode table ------ #


class FileTableInode(TableSpec):
    inode_id: Annotated[int, ConstrainRepr("PRIMARY KEY"), SkipValidation]
    uid: Annotated[int, ConstrainRepr("NOT NULL"), SkipValidation]
    gid: Annotated[int, ConstrainRepr("NOT NULL"), SkipValidation]
    mode: Annotated[int, ConstrainRepr("NOT NULL"), SkipValidation]
    links_count: Annotated[Optional[int], SkipValidation] = None
    # NOTE: due to legacy OTA image doesn't support xattrs, we
    #       just don't use this field for now.
    xattrs: Annotated[Optional[bytes], SkipValidation] = None


class FiletableInodeTypedDict(TypedDict, total=False):
    inode_id: int
    uid: int
    gid: int
    mode: int
    links_count: Optional[int]
    xattrs: Optional[bytes]


class FileTableInodeORM(ORMBase[FileTableInode]):
    orm_bootstrap_table_name = FT_INODE_TABLE_NAME
    orm_bootstrap_create_table_params = CreateTableParams(without_rowid=False)


# ------ regular file table ------ #


class FileTableRegularFiles(TableSpec):
    """DB table for regular file entries."""

    path: Annotated[str, ConstrainRepr("PRIMARY KEY"), SkipValidation]
    inode_id: Annotated[int, ConstrainRepr("NOT NULL"), SkipValidation]
    resource_id: Annotated[int, SkipValidation]


class FileTableRegularTypedDict(TypedDict, total=False):
    path: str
    inode_id: int
    resource_id: int


class FileTableRegularORM(ORMBase[FileTableRegularFiles]):
    orm_bootstrap_table_name = FT_REGULAR_TABLE_NAME
    orm_bootstrap_create_table_params = CreateTableParams(without_rowid=True)
    orm_bootstrap_indexes_params = [
        CreateIndexParams(
            index_name="fr_resource_id_index", index_cols=("resource_id",)
        ),
        CreateIndexParams(index_name="fr_inode_id_index", index_cols=("inode_id",)),
    ]


class FileTableRegularORMPool(ORMThreadPoolBase[FileTableRegularFiles]):
    orm_bootstrap_table_name = FT_REGULAR_TABLE_NAME


# ------ non-regular file table ------ #


class FileTableNonRegularFiles(TableSpec):
    """DB table for non-regular file entries.

    This includes:
    1. symlink.
    2. chardev file.

    NOTE that support for chardev file is only for overlayfs' whiteout file,
        so only device num as 0,0 will be allowed.
    NOTE: chardev is not supported by legacy OTA image, so just ignore it.
    """

    path: Annotated[str, ConstrainRepr("PRIMARY KEY"), SkipValidation]
    inode_id: Annotated[int, ConstrainRepr("NOT NULL"), SkipValidation]
    meta: Annotated[Optional[bytes], SkipValidation] = None
    """The contents of the file. Currently only used by symlink."""


class FileTableNonRegularTypedDict(TypedDict, total=False):
    path: str
    inode_id: int
    meta: Optional[bytes]


class FileTableNonRegularORM(ORMBase[FileTableNonRegularFiles]):
    orm_bootstrap_table_name = FT_NON_REGULAR_TABLE_NAME
    orm_bootstrap_create_table_params = CreateTableParams(without_rowid=True)
    orm_bootstrap_indexes_params = [
        CreateIndexParams(index_name="fnr_inode_id_index", index_cols=("inode_id",)),
    ]


# ------ directory table ------ #


class FileTableDirectories(TableSpec):
    path: Annotated[str, ConstrainRepr("PRIMARY KEY"), SkipValidation]
    inode_id: Annotated[int, ConstrainRepr("NOT NULL"), SkipValidation]


class FileTableDirectoryTypedDict(TypedDict, total=False):
    path: str
    inode_id: int


class FileTableDirORM(ORMBase[FileTableDirectories]):
    orm_bootstrap_table_name = FT_DIR_TABLE_NAME
    orm_bootstrap_create_table_params = CreateTableParams(without_rowid=True)
    orm_bootstrap_indexes_params = [
        CreateIndexParams(index_name="fd_inode_id_index", index_cols=("inode_id",)),
    ]


# ------ resource table ------ #


class FileTableResource(TableSpec):
    resource_id: Annotated[int, ConstrainRepr("PRIMARY KEY"), SkipValidation]
    digest: Annotated[bytes, ConstrainRepr("NOT NULL"), SkipValidation]
    size: Annotated[int, ConstrainRepr("NOT NULL"), SkipValidation]


class FileTableResourceTypedDict(TypedDict, total=False):
    resource_id: int
    digest: bytes
    size: int


class FileTableResourceORM(ORMBase[FileTableResource]):
    orm_bootstrap_table_name = FT_RESOURCE_TABLE_NAME
    orm_bootstrap_create_table_params = CreateTableParams(without_rowid=False)
    orm_bootstrap_indexes_params = [
        CreateIndexParams(index_name="frs_digest_index", index_cols=("digest",))
    ]


class FileTableResourceORMPool(ORMThreadPoolBase[FileTableResource]):
    orm_bootstrap_table_name = FT_RESOURCE_TABLE_NAME
