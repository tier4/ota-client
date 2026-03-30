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
from typing import TypedDict
from urllib.parse import quote

import aiohttp


class DownloadResult(TypedDict):
    total: int
    ok: int
    failed_downloads: list[str]
    hash_mismatches: list[str]


async def run(
    proxy_url: str,
    upstream_url: str,
    blobs: dict[str, str],
    extra_headers: dict[str, str],
) -> DownloadResult:
    failed_downloads: list[str] = []
    hash_mismatches: list[str] = []
    ok = 0

    async with aiohttp.ClientSession() as session:
        for name, expected_sha256 in blobs.items():
            url = f"{upstream_url}/{quote(name)}"
            try:
                async with session.get(
                    url, proxy=proxy_url, headers=extra_headers
                ) as resp:
                    if resp.status != 200:
                        failed_downloads.append(f"{name}: HTTP {resp.status}")
                        continue
                    h = hashlib.sha256()
                    async for chunk in resp.content.iter_any():
                        h.update(chunk)

                    actual = h.hexdigest()
                    if actual != expected_sha256:
                        hash_mismatches.append(
                            f"{name}: expected {expected_sha256}, got {actual}"
                        )
                    else:
                        ok += 1
            except Exception as e:
                failed_downloads.append(f"{name}: {e!r}")

    return DownloadResult(
        total=len(blobs),
        ok=ok,
        failed_downloads=failed_downloads,
        hash_mismatches=hash_mismatches,
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
