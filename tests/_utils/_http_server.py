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
"""Standalone HTTP server used by e2e tests.

Usage:
    python _http_server.py --port PORT --directory DIR [--host HOST]

Serves files under DIR; the URL path maps directly to the file path
(e.g. GET /abc123 → DIR/abc123). Prints ``READY:<port>`` on stdout once
the listening socket is bound so the parent process can synchronize.
"""

from __future__ import annotations

import argparse
import sys
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        # Suppress request logging to keep test output clean.
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone e2e HTTP server")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--directory", type=str, required=True)
    args = parser.parse_args()

    serve_dir = Path(args.directory)
    if not serve_dir.is_dir():
        print(f"Error: directory {serve_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    handler = partial(_QuietHandler, directory=str(serve_dir))
    server = HTTPServer((args.host, args.port), handler)

    # Signal readiness to parent process via stdout.
    print(f"READY:{args.port}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
