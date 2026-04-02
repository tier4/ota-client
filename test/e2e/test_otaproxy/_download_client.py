#!/usr/bin/env python3
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
"""Standalone download client for OTA proxy e2e testing.

Downloads all blobs listed in a JSON manifest through an HTTP proxy,
validates SHA256 integrity, and reports results as JSON on stdout.

Usage:
    python download_client.py \
        --proxy-url http://127.0.0.1:18080 \
        --upstream-url http://127.0.0.1:18888 \
        --manifest /tmp/manifest.json \
        [--header "ota-file-cache-control: no_cache"]

Manifest format (JSON):
    {"<filename>": "<expected_sha256_hex>", ...}

Output (JSON on stdout):
    {
        "total": <int>,
        "ok": <int>,
        "failed_downloads": ["<filename>: HTTP <status>", ...],
        "hash_mismatches": ["<filename>: expected <exp>, got <act>", ...]
    }

Exit codes:
    0 - all downloads succeeded and validated
    1 - some downloads failed or had hash mismatches
    2 - usage / startup error
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from enum import Enum
from typing import TypedDict
from urllib.parse import quote

import aiohttp

from ota_proxy.cache_control_header import HEADER_LOWERCASE as OTA_CACHE_CONTROL_HEADER
from ota_proxy.cache_control_header import (
    export_kwargs_as_header_string,
    update_header_str,
)

# When cache rotation engaged, cache file might be
# unavailable temporarily, allow retry on this case.
RETRY_COUNT = 6


class DownloadErrorType(Enum):
    failed_downloaded = 0
    hash_mismatches = 1


class DownloadResult(TypedDict):
    total: int
    ok: int
    failed_downloads: list[str]
    hash_mismatches: list[str]
    recorded_failed: list[str]
    """Include retried failed downloads."""


def _set_retry_caching(headers: dict[str, str]) -> None:
    """Add retry_caching directive to the ota-file-cache-control header."""
    headers[OTA_CACHE_CONTROL_HEADER] = update_header_str(
        headers[OTA_CACHE_CONTROL_HEADER],
        retry_caching=True,
    )


async def run(  # noqa
    proxy_url: str,
    upstream_url: str,
    blobs: dict[str, str],
    extra_headers: dict[str, str],
) -> DownloadResult:
    failed_downloads: list[str] = []
    hash_mismatches: list[str] = []
    recorded_failed: list[str] = []
    ok = 0

    async with aiohttp.ClientSession() as session:
        for name, expected_sha256 in blobs.items():
            url = f"{upstream_url}/{quote(name)}"
            # Send ota-file-cache-control with file_sha256 so the proxy
            # uses the actual digest as cache key (matching real otaclient
            # behavior via inject_cache_control_header_in_req).
            per_file_headers = dict(extra_headers)
            per_file_headers[OTA_CACHE_CONTROL_HEADER] = export_kwargs_as_header_string(
                file_sha256=expected_sha256
            )

            last_err, err_type = None, None
            headers = dict(per_file_headers)
            for _ in range(RETRY_COUNT):
                try:
                    async with session.get(
                        url, proxy=proxy_url, headers=headers
                    ) as resp:
                        if resp.status != 200:
                            last_err = f"{name}: HTTP {resp.status}"
                            err_type = DownloadErrorType.failed_downloaded
                            recorded_failed.append(last_err)
                            _set_retry_caching(headers)
                            continue

                        h = hashlib.sha256()
                        async for chunk in resp.content.iter_any():
                            h.update(chunk)

                        actual = h.hexdigest()
                        if actual != expected_sha256:
                            last_err = (
                                f"{name}: expected {expected_sha256}, got {actual}"
                            )
                            err_type = DownloadErrorType.hash_mismatches
                            recorded_failed.append(last_err)
                            _set_retry_caching(headers)
                        else:
                            ok += 1
                            break
                except Exception as e:
                    last_err = f"{name}: {e!r}"
                    err_type = DownloadErrorType.failed_downloaded
                    recorded_failed.append(last_err)
                    _set_retry_caching(headers)

                await asyncio.sleep(1)

            if err_type == DownloadErrorType.failed_downloaded and last_err:
                failed_downloads.append(last_err)
            if err_type == DownloadErrorType.hash_mismatches and last_err:
                hash_mismatches.append(last_err)

    return DownloadResult(
        total=len(blobs),
        ok=ok,
        failed_downloads=failed_downloads,
        hash_mismatches=hash_mismatches,
        recorded_failed=recorded_failed,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="OTA proxy download client")
    parser.add_argument("--proxy-url", required=True)
    parser.add_argument("--upstream-url", required=True)
    parser.add_argument("--manifest", required=True, help="Path to JSON manifest file")
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        help="Extra header as 'Name: Value' (repeatable)",
    )
    parser.add_argument(
        "--start-at",
        type=float,
        default=0,
        help="Unix timestamp to wait for before starting downloads",
    )
    args = parser.parse_args()

    try:
        with open(args.manifest) as f:
            blobs: dict[str, str] = json.load(f)
    except Exception as e:
        print(f"Error reading manifest: {e}", file=sys.stderr)
        sys.exit(2)

    extra_headers: dict[str, str] = {}
    for h in args.header:
        if ": " not in h:
            print(
                f"Invalid header format (expected 'Name: Value'): {h}", file=sys.stderr
            )
            sys.exit(2)
        k, v = h.split(": ", 1)
        extra_headers[k] = v

    if args.start_at > 0:
        wait = args.start_at - time.time()
        if wait > 0:
            time.sleep(wait)

    result = asyncio.run(run(args.proxy_url, args.upstream_url, blobs, extra_headers))
    print(json.dumps(result))

    if result["failed_downloads"] or result["hash_mismatches"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
