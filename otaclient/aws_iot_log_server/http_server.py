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


import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from queue import Queue


class HttpHandler(BaseHTTPRequestHandler):
    _queue = Queue()

    def do_POST(self):
        parsed = urlparse(self.path)
        content_len = int(self.headers.get("content-length", 0))
        req_body = self.rfile.read(content_len).decode("utf-8")
        path_list = list(filter(lambda x: x, parsed.path.split("/")))
        self._queue.put(
            {
                "timestamp": int(time.time()) * 1000,
                "path": path_list,
                "message": req_body,
            }
        )
        self.send_response(200)
        self.end_headers()


if __name__ == "__main__":
    server = HTTPServer(("localhost", 8080), HttpHandler)
    server.serve_forever()
