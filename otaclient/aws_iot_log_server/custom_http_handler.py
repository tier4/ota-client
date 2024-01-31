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
import requests
import logging
import logging.handlers
from urllib.parse import urlparse
from queue import Queue
from threading import Thread
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)


class CustomHttpHandler(logging.Handler):
    # https://stackoverflow.com/questions/62957814/python-logging-log-data-to-server-using-the-logging-module
    def __init__(self, url: str, timeout=3.0, *, max_queue_len=4096):
        """
        only method post is supported
        secure(=https) is not supported
        """
        super().__init__()
        _parsed_url = urlparse(url)
        self.url = _parsed_url._replace(scheme="http").geturl()

        self.timeout = timeout
        self.queue = Queue(maxsize=max_queue_len)

        self.thread = Thread(target=self._start, daemon=True)
        self.thread.start()

    def emit(self, record):
        """Get a record from logging module and put it into self.queue."""
        _queue = self.queue
        with _queue.mutex:
            if 0 < _queue.maxsize <= _queue._qsize():
                _queue._get()  # drop one oldest item from list
            _queue._put(self.format(record))

    def _start(self):
        session = requests.Session()
        retries = Retry(total=5, backoff_factor=1)

        # NOTE: for urllib3 version below 2.0, there is no way to set
        #       backoff_max when instanitiate Retry object.
        Retry.DEFAULT_BACKOFF_MAX = 10  # type: ignore

        session.mount("http://", HTTPAdapter(max_retries=retries))
        while True:
            log_entry = self.queue.get()
            try:
                with session.post(
                    url=self.url,
                    data=log_entry,
                    timeout=self.timeout,
                ):
                    pass
            except Exception:
                pass
