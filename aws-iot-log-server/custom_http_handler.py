import requests
import logging
import logging.handlers


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

    def emit(self, record):
        log_entry = self.format(record)
        return requests.post(
            f"http://{self.host}{self.url}", log_entry, timeout=self.timeout
        ).content


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
