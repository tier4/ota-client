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


import requests
import logging
import logging.handlers
from queue import Queue, Empty
from threading import Thread
from requests.packages.urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)


class CustomHttpHandler(logging.Handler):
    # https://stackoverflow.com/questions/62957814/python-logging-log-data-to-server-using-the-logging-module
    def __init__(self, host, url, timeout=3.0):
        """
        only method post is supported
        secure(=https) is not supported
        """
        super().__init__()
        self.host = host
        self.url = url if url.startswith("/") else f"/{url}"
        self.timeout = timeout
        self.queue = Queue(maxsize=4096)
        self.thread = Thread(target=self._start)
        self.thread.start()

    def emit(self, record):
        log_entry = self.format(record)
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except Empty:
                pass
        try:
            self.queue.put_nowait(log_entry)
        except Empty:
            pass

    def _start(self):
        session = requests.Session()
        retries = Retry(total=5, backoff_factor=1)  # 0, 1, 2, 4, 8
        Retry.DEFAULT_BACKOFF_MAX = 10
        session.mount("http://", HTTPAdapter(max_retries=retries))
        while True:
            log_entry = self.queue.get()
            try:
                session.post(
                    url=f"http://{self.host}{self.url}",
                    data=log_entry,
                    timeout=self.timeout,
                )
            except Exception:
                logger.exception("post failure")


if __name__ == "__main__":
    from configs import LOG_FORMAT

    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(fmt=LOG_FORMAT)

    # stream handler
    _sh = logging.StreamHandler()
    _sh.setFormatter(fmt)

    _hh = CustomHttpHandler(host="localhost:8080", url="my-ecu-id-123")
    _hh.setFormatter(fmt)

    logger.addHandler(_sh)
    logger.addHandler(_hh)

    logger.info("123")
    logger.info("xyz")
