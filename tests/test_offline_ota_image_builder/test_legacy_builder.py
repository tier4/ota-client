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
"""Unit tests for the legacy _process_ota_image function in offline_ota_image_builder."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import zstandard

# NOTE: sys.path is patched by conftest.py so that tools.* is importable.
from tools.offline_ota_image_builder.builder import _process_ota_image
from tools.offline_ota_image_builder.configs import cfg

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SHA256 = hashlib.sha256


def _sha256hex(data: bytes) -> str:
    return _SHA256(data).hexdigest()


def _fake_jwt(payload_dict: dict) -> str:
    """Create a minimal (unsigned) JWT for testing."""
    header = base64.urlsafe_b64encode(b'{"alg":"ES256"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps(payload_dict).encode())
        .rstrip(b"=")
        .decode()
    )
    sig = base64.urlsafe_b64encode(b"fakesig").rstrip(b"=").decode()
    return f"{header}.{payload}.{sig}"


def _build_legacy_ota_image(
    root: Path,
    uncompressed: dict[str, bytes],
    compressed: dict[str, bytes],
) -> tuple[dict[str, str], dict[str, str]]:
    """Populate *root* as a minimal legacy OTA image.

    Args:
        root: target directory (must already exist)
        uncompressed: ``{abs_path: content}`` – regular (uncompressed) files
        compressed:   ``{abs_path: content}`` – files to store compressed

    Returns:
        (uncompressed_sha_map, compressed_sha_map) mapping path → sha256 hex.
    """
    data_dir = root / "data"
    data_dir.mkdir()
    data_zst_dir = root / "data.zst"
    data_zst_dir.mkdir()

    uncompressed_sha: dict[str, str] = {}
    compressed_sha: dict[str, str] = {}
    regular_csv_lines: list[str] = []

    # --- uncompressed blobs: stored under data/<rel_path> ---
    for path, content in uncompressed.items():
        sha = _sha256hex(content)
        uncompressed_sha[path] = sha
        fpath = data_dir / Path(path).relative_to("/")
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_bytes(content)
        regular_csv_lines.append(f"0644,1000,1000,1,{sha},'{path}',{len(content)}")

    # --- compressed blobs: stored under data.zst/<sha256>.zst ---
    cctx = zstandard.ZstdCompressor()
    for path, content in compressed.items():
        sha = _sha256hex(content)
        compressed_sha[path] = sha
        blob_name = f"{sha}.{cfg.OTA_IMAGE_COMPRESSION_ALG}"
        (data_zst_dir / blob_name).write_bytes(cctx.compress(content))
        regular_csv_lines.append(
            f"0644,1000,1000,1,{sha},'{path}',{len(content)},,{cfg.OTA_IMAGE_COMPRESSION_ALG}"
        )

    # --- metafiles ---
    (root / "regulars.txt").write_text("\n".join(regular_csv_lines) + "\n")
    (root / "dirs.txt").write_text("")
    (root / "symlinks.txt").write_text("")
    (root / "persistents.txt").write_text("")
    (root / "sign.pem").write_text("DUMMY_CERT")

    # --- metadata.jwt (legacy v1 list-of-dicts format) ---
    payload = [
        {"version": 1},
        {"directory": "dirs.txt", "hash": "dummy"},
        {"symboliclink": "symlinks.txt", "hash": "dummy"},
        {"regular": "regulars.txt", "hash": "dummy"},
        {"persistent": "persistents.txt", "hash": "dummy"},
        {"certificate": "sign.pem", "hash": "dummy"},
        {"rootfs_directory": "data"},
        {"compressed_rootfs_directory": "data.zst"},
    ]
    (root / "metadata.jwt").write_text(_fake_jwt(payload))

    return uncompressed_sha, compressed_sha


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProcessOtaImageLegacy:
    """Unit tests for the legacy _process_ota_image function."""

    # ------------------------------------------------------------------
    # Uncompressed files
    # ------------------------------------------------------------------

    def test_uncompressed_file_copied_to_data_by_sha256(self, tmp_path: Path):
        """Uncompressed blob must appear at data/<sha256> with original content."""
        image_dir = tmp_path / "image"
        image_dir.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()

        content = b"hello uncompressed"
        sha = _sha256hex(content)

        _build_legacy_ota_image(
            image_dir,
            uncompressed={"/usr/bin/testfile": content},
            compressed={},
        )

        _process_ota_image(image_dir, data_dir=data_dir, meta_dir=meta_dir)

        assert (data_dir / sha).is_file(), "blob not found in data/"
        assert (data_dir / sha).read_bytes() == content

    def test_uncompressed_nested_path(self, tmp_path: Path):
        """File under a deep nested path should resolve via its sha256 name."""
        image_dir = tmp_path / "image"
        image_dir.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()

        content = b"nested file content"
        sha = _sha256hex(content)

        _build_legacy_ota_image(
            image_dir,
            uncompressed={"/usr/lib/x86_64-linux-gnu/libfoo.so.1": content},
            compressed={},
        )

        _process_ota_image(image_dir, data_dir=data_dir, meta_dir=meta_dir)

        assert (data_dir / sha).is_file()

    # ------------------------------------------------------------------
    # Compressed files
    # ------------------------------------------------------------------

    def test_compressed_file_copied_with_zst_suffix(self, tmp_path: Path):
        """Compressed blob must appear at data/<sha256>.zst (zstd format)."""
        image_dir = tmp_path / "image"
        image_dir.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()

        content = b"compressed content here " * 100
        sha = _sha256hex(content)

        _build_legacy_ota_image(
            image_dir,
            uncompressed={},
            compressed={"/usr/lib/big.so": content},
        )

        _process_ota_image(image_dir, data_dir=data_dir, meta_dir=meta_dir)

        expected = f"{sha}.{cfg.OTA_IMAGE_COMPRESSION_ALG}"
        assert (data_dir / expected).is_file(), "compressed blob not found"

        # Verify the stored blob actually decompresses to the original content
        dctx = zstandard.ZstdDecompressor()
        assert dctx.decompress((data_dir / expected).read_bytes()) == content

    def test_compressed_original_not_in_data(self, tmp_path: Path):
        """The uncompressed sha256 name (without .zst) must NOT appear in data/."""
        image_dir = tmp_path / "image"
        image_dir.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()

        content = b"some large file " * 200
        sha = _sha256hex(content)

        _build_legacy_ota_image(
            image_dir,
            uncompressed={},
            compressed={"/var/lib/big.bin": content},
        )

        _process_ota_image(image_dir, data_dir=data_dir, meta_dir=meta_dir)

        assert not (
            data_dir / sha
        ).is_file(), "raw sha should not appear for compressed"

    # ------------------------------------------------------------------
    # Mixed
    # ------------------------------------------------------------------

    def test_mixed_files(self, tmp_path: Path):
        """Both compressed and uncompressed files in one image."""
        image_dir = tmp_path / "image"
        image_dir.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()

        unc = {"/etc/config": b"cfg data", "/usr/bin/tool": b"tool binary"}
        cmp = {"/usr/lib/big.so": b"biglib" * 200}

        _build_legacy_ota_image(image_dir, uncompressed=unc, compressed=cmp)
        _process_ota_image(image_dir, data_dir=data_dir, meta_dir=meta_dir)

        for content in unc.values():
            assert (data_dir / _sha256hex(content)).is_file()
        for content in cmp.values():
            name = f"{_sha256hex(content)}.{cfg.OTA_IMAGE_COMPRESSION_ALG}"
            assert (data_dir / name).is_file()

    # ------------------------------------------------------------------
    # Metafiles
    # ------------------------------------------------------------------

    def test_metafiles_moved_to_meta_dir(self, tmp_path: Path):
        """All OTA metafiles (including certificate and metadata.jwt) must appear in meta/."""
        image_dir = tmp_path / "image"
        image_dir.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()

        _build_legacy_ota_image(image_dir, uncompressed={"/a": b"a"}, compressed={})
        _process_ota_image(image_dir, data_dir=data_dir, meta_dir=meta_dir)

        for fname in [
            "metadata.jwt",
            "regulars.txt",
            "dirs.txt",
            "symlinks.txt",
            "persistents.txt",
            "sign.pem",
        ]:
            assert (meta_dir / fname).is_file(), f"{fname} not in meta/"

    def test_metafiles_removed_from_image_dir(self, tmp_path: Path):
        """Metafiles must be *moved* (not copied) – they should not remain in image_dir."""
        image_dir = tmp_path / "image"
        image_dir.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()

        _build_legacy_ota_image(image_dir, uncompressed={"/a": b"a"}, compressed={})
        _process_ota_image(image_dir, data_dir=data_dir, meta_dir=meta_dir)

        for fname in ["metadata.jwt", "regulars.txt", "sign.pem"]:
            assert not (image_dir / fname).is_file(), f"{fname} still in image_dir"

    # ------------------------------------------------------------------
    # Return values
    # ------------------------------------------------------------------

    def test_returns_correct_file_count_uncompressed(self, tmp_path: Path):
        image_dir = tmp_path / "image"
        image_dir.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()

        unc = {"/a": b"aaa", "/b": b"bbbb", "/c": b"ccccc"}
        _build_legacy_ota_image(image_dir, uncompressed=unc, compressed={})

        count, size = _process_ota_image(
            image_dir, data_dir=data_dir, meta_dir=meta_dir
        )

        assert count == len(unc)
        assert size == sum(len(c) for c in unc.values())

    def test_returns_correct_size_compressed(self, tmp_path: Path):
        """Reported size for compressed files is the size of the .zst blob."""
        image_dir = tmp_path / "image"
        image_dir.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()

        content = b"compressible data " * 500
        sha = _sha256hex(content)
        _build_legacy_ota_image(
            image_dir, uncompressed={}, compressed={"/big": content}
        )

        count, size = _process_ota_image(
            image_dir, data_dir=data_dir, meta_dir=meta_dir
        )

        assert count == 1
        # The saved size is the compressed blob size, not original
        expected_name = f"{sha}.{cfg.OTA_IMAGE_COMPRESSION_ALG}"
        assert size == (data_dir / expected_name).stat().st_size

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def test_duplicate_hash_stored_only_once(self, tmp_path: Path):
        """Two files with identical content share one blob in data/."""
        image_dir = tmp_path / "image"
        image_dir.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()

        same = b"same content"
        unc = {"/a/x": same, "/b/y": same}
        _build_legacy_ota_image(image_dir, uncompressed=unc, compressed={})

        count, _ = _process_ota_image(image_dir, data_dir=data_dir, meta_dir=meta_dir)

        sha = _sha256hex(same)
        assert (data_dir / sha).is_file(), "deduped blob missing"
        # Only one blob in data/
        blobs = list(data_dir.iterdir())
        assert len(blobs) == 1
        # count ≤ 2 (first copy is moved, second src no longer exists)
        assert count <= len(unc)
