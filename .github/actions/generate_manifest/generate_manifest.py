import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone

from jsonschema import ValidationError, validate

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

    return str(os.path.getsize(file_path))


def generate_manifest(input_dir):
    packages = []
    for root, _, files in os.walk(input_dir):
        for file in files:
            if not (file.endswith(".squashfs") or file.endswith(".patch")):
                continue

            file_path = os.path.join(root, file)
            file_info = {
                "filename": file,
                "type": "squashfs" if file.endswith(".squashfs") else "patch",
                "architecture": "x86_64" if "x86_64" in file else "arm64",
                "size": get_file_size(file_path),
                "checksum": calculate_checksum(file_path),
            }
            if file_info["type"] == "squashfs":
                # squashfs file name format: ota-client-${architecture}-v${version}.squashfs
                match = re.search(r"v(\d+\.\d+\.\d+)", file)
                file_info["version"] = match.group(1) if match else "unknown"
            else:
                # patch file name format: ota-client-${architecture}_v${BASE_VERSION}-v${VERSION}.patch
                match = re.search(r"v(\d+\.\d+\.\d+)-v(\d+\.\d+\.\d+)", file)
                file_info["base_version"] = match.group(1) if match else "unknown"
                file_info["version"] = match.group(2) if match else "unknown"

            packages.append(file_info)

    data = {
        "schema_version": SCHEMA_VERSION,
        "date": datetime.now(timezone.utc).isoformat(),
        "packages": packages,
    }
    return data


def validate_manifest(data, schema_file):
    """
    Validate the manifest.json file against the schema.
    """

    with open(schema_file) as file_obj:
        json_schema = json.load(file_obj)

    try:
        validate(data, json_schema)
    except ValidationError as e:
        print(e.message)


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
    parser.add_argument("--schema", type=str, required=False, help="Schema file")
    parser.add_argument("--output", type=str, required=True, help="Output file name")

    args = parser.parse_args()
    input_dir = args.dir
    schema_file = args.schema
    output_file = args.output

    data = generate_manifest(input_dir)
    with open(output_file, "w") as f:
        json.dump(data, f, indent=4)

    if schema_file:
        validate_manifest(data, schema_file)


if __name__ == "__main__":
    main()
