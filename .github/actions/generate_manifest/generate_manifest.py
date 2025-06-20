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
"""Generate a manifest.json file for the OTA client."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
from datetime import datetime, timezone

from otaclient_manifest.schema import Manifest, PackageExtraMetadata, ReleasePackage

SCHEMA_VERSION = "1"


def calculate_checksum(file_path):
    """
    Calculate the sha256 checksum of the file.
    """

    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return "sha256:" + sha256_hash.hexdigest()


def get_file_size(file_path):
    """
    Get the size of the file.
    """

    return os.path.getsize(file_path)


def generate_manifest(input_dir: str):
    packages = []
    for root, _, files in os.walk(input_dir):
        for file in files:
            if not (file.endswith(".squashfs") or file.endswith(".patch")):
                continue

            file_path = os.path.join(root, file)
            file_type = "squashfs" if file.endswith(".squashfs") else "patch"
            if file_type == "squashfs":
                # squashfs file name format: otaclient-${architecture}-v${version}.squashfs
                # Supports pre-release versions like: v0.1.dev1, v3.10.0rc1, etc.
                match = re.search(r"v(\d+\.\d+(?:\.\d+)?(?:\.[a-zA-Z0-9]+)?)", file)
                _base_version = None
                version = match.group(1) if match else "unknown"
            else:
                # patch file name format: otaclient-${architecture}_v${BASE_VERSION}-v${VERSION}.patch
                # Supports pre-release versions in both base and target versions
                match = re.search(
                    r"v(\d+\.\d+(?:\.\d+)?(?:\.[a-zA-Z0-9]+)?)-v(\d+\.\d+(?:\.\d+)?(?:\.[a-zA-Z0-9]+)?)",
                    file,
                )
                _base_version = match.group(1) if match else "unknown"
                version = match.group(2) if match else "unknown"

            package = ReleasePackage(
                filename=file,
                type=file_type,
                version=version,
                architecture="x86_64" if "x86_64" in file else "arm64",
                size=get_file_size(file_path),
                checksum=calculate_checksum(file_path),
            )

            if _base_version:
                metadata = PackageExtraMetadata(patch_base_version=_base_version)
                package.metadata = metadata

            packages.append(package)

    return Manifest(
        schema_version=SCHEMA_VERSION,
        date=datetime.now(timezone.utc),
        packages=packages,
    )


def main():
    """
    Generate a manifest.json file of the artifact(squashfs and patch).
    """

    parser = argparse.ArgumentParser(description="Generate a manifest.json file")
    parser.add_argument(
        "--dir",
        type=str,
        required=True,
        help="Directory containing the artifact(squashfs, patch)",
    )
    parser.add_argument("--output", type=str, required=True, help="Output file name")

    args = parser.parse_args()
    input_dir: str = args.dir
    output_file: str = args.output

    data = generate_manifest(input_dir)
    # NOTE: when JSON serialing, datetime by default will be serialized into
    #       ISO8601 format string, like the following:
    #           2025-02-04T03:22:35.871745
    with open(output_file, "w") as f:
        f.write(data.model_dump_json(indent=4, exclude_unset=True))


if __name__ == "__main__":
    main()
