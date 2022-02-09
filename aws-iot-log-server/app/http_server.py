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
